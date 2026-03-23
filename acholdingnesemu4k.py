#!/usr/bin/env python3
"""
AC NES Emu 0.1 – Complete NES emulator in a single file.
MOS 6502 CPU · RP2C02 PPU · Mappers 0/1/2/3 · tkinter GUI

Controls:
    Z / X       = A / B
    A / S       = Select / Start
    Arrow keys  = D-pad
    P           = Pause / Resume
    R           = Reset ROM

Requirements:
    pip install numpy pillow
    (tkinter ships with most Python installs)
"""

import numpy as np
import time
import os
import traceback


# ═════════════════════════════════════════════════════════════════════════════
#  NTSC 2C02 PALETTE  (64 colours, RGB)
# ═════════════════════════════════════════════════════════════════════════════

NES_PALETTE = [
    (84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),
    (84,4,0),(60,24,0),(32,42,0),(8,58,0),(0,64,0),(0,60,0),
    (0,50,60),(0,0,0),(0,0,0),(0,0,0),
    (152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),(160,20,100),
    (152,34,32),(120,60,0),(84,90,0),(40,114,0),(8,124,0),(0,118,40),
    (0,102,120),(0,0,0),(0,0,0),(0,0,0),
    (236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),(236,88,180),
    (236,106,100),(212,136,32),(160,170,0),(116,196,0),(76,208,32),(56,204,108),
    (56,180,204),(60,60,60),(0,0,0),(0,0,0),
    (236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),(236,174,212),
    (236,180,176),(228,196,144),(204,210,120),(180,222,120),(168,226,144),(152,226,180),
    (160,214,228),(160,162,160),(0,0,0),(0,0,0),
]


# ═════════════════════════════════════════════════════════════════════════════
#  MOS 6502 CPU
# ═════════════════════════════════════════════════════════════════════════════

C_FLAG = 0x01
Z_FLAG = 0x02
I_FLAG = 0x04
D_FLAG = 0x08
B_FLAG = 0x10
U_FLAG = 0x20
V_FLAG = 0x40
N_FLAG = 0x80


class CPU:
    """MOS 6502 – all official + common unofficial opcodes."""

    __slots__ = (
        'a', 'x', 'y', 'pc', 'sp', 'status',
        'cycles', 'total_cycles', 'bus',
        'nmi_pending', 'irq_pending', 'stall',
        '_opcode_table',
    )

    def __init__(self, bus):
        self.a = 0
        self.x = 0
        self.y = 0
        self.pc = 0
        self.sp = 0xFD
        self.status = 0x24
        self.cycles = 0
        self.total_cycles = 0
        self.bus = bus
        self.nmi_pending = False
        self.irq_pending = False
        self.stall = 0
        self._build_opcode_table()

    # ── helpers ──────────────────────────────────────────────────────────────
    @property
    def p(self):
        return self.status

    def _set_zn(self, v):
        self.status = (self.status & ~(Z_FLAG | N_FLAG)) | \
                      (Z_FLAG if (v & 0xFF) == 0 else 0) | (v & N_FLAG)

    def _read(self, addr):
        return self.bus.cpu_read(addr & 0xFFFF)

    def _write(self, addr, val):
        self.bus.cpu_write(addr & 0xFFFF, val & 0xFF)

    def _push(self, val):
        self._write(0x0100 | self.sp, val & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def _pull(self):
        self.sp = (self.sp + 1) & 0xFF
        return self._read(0x0100 | self.sp)

    def _push16(self, val):
        self._push((val >> 8) & 0xFF)
        self._push(val & 0xFF)

    def _pull16(self):
        lo = self._pull()
        hi = self._pull()
        return (hi << 8) | lo

    def _read16(self, addr):
        lo = self._read(addr)
        hi = self._read((addr + 1) & 0xFFFF)
        return (hi << 8) | lo

    def _read16_bug(self, addr):
        lo = self._read(addr)
        hi_addr = (addr & 0xFF00) | ((addr + 1) & 0xFF)
        hi = self._read(hi_addr)
        return (hi << 8) | lo

    def _branch(self, cond):
        offset = self._read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        if cond:
            self.cycles += 1
            old_pc = self.pc
            if offset >= 0x80:
                offset -= 0x100
            self.pc = (self.pc + offset) & 0xFFFF
            if (old_pc & 0xFF00) != (self.pc & 0xFF00):
                self.cycles += 1

    def _pages_differ(self, a, b):
        return (a & 0xFF00) != (b & 0xFF00)

    # ── addressing modes ─────────────────────────────────────────────────────
    def _am_imp(self):  return 0
    def _am_acc(self):  return 0

    def _am_imm(self):
        addr = self.pc; self.pc = (self.pc + 1) & 0xFFFF; return addr

    def _am_zp(self):
        addr = self._read(self.pc) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF; return addr

    def _am_zpx(self):
        addr = (self._read(self.pc) + self.x) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF; return addr

    def _am_zpy(self):
        addr = (self._read(self.pc) + self.y) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF; return addr

    def _am_abs(self):
        addr = self._read16(self.pc); self.pc = (self.pc + 2) & 0xFFFF; return addr

    def _am_abx(self, check_page=True):
        base = self._read16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        addr = (base + self.x) & 0xFFFF
        if check_page and self._pages_differ(base, addr): self.cycles += 1
        return addr

    def _am_abx_nopage(self):
        return self._am_abx(False)

    def _am_aby(self, check_page=True):
        base = self._read16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        addr = (base + self.y) & 0xFFFF
        if check_page and self._pages_differ(base, addr): self.cycles += 1
        return addr

    def _am_aby_nopage(self):
        return self._am_aby(False)

    def _am_ind(self):
        ptr = self._read16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        return self._read16_bug(ptr)

    def _am_izx(self):
        base = (self._read(self.pc) + self.x) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF
        lo = self._read(base); hi = self._read((base + 1) & 0xFF)
        return (hi << 8) | lo

    def _am_izy(self, check_page=True):
        base = self._read(self.pc); self.pc = (self.pc + 1) & 0xFFFF
        lo = self._read(base); hi = self._read((base + 1) & 0xFF)
        addr_base = (hi << 8) | lo
        addr = (addr_base + self.y) & 0xFFFF
        if check_page and self._pages_differ(addr_base, addr): self.cycles += 1
        return addr

    def _am_izy_nopage(self):
        return self._am_izy(False)

    # ── instructions ─────────────────────────────────────────────────────────
    def _op_adc(self, addr):
        val = self._read(addr); a = self.a; c = 1 if (self.status & C_FLAG) else 0
        result = a + val + c
        self.status &= ~(C_FLAG | V_FLAG)
        if result > 0xFF: self.status |= C_FLAG
        if ((a ^ result) & (val ^ result) & 0x80): self.status |= V_FLAG
        self.a = result & 0xFF; self._set_zn(self.a)

    def _op_and(self, addr):
        self.a &= self._read(addr); self._set_zn(self.a)

    def _op_asl_acc(self, addr):
        self.status = (self.status & ~C_FLAG) | ((self.a >> 7) & 1)
        self.a = (self.a << 1) & 0xFF; self._set_zn(self.a)

    def _op_asl_mem(self, addr):
        val = self._read(addr)
        self.status = (self.status & ~C_FLAG) | ((val >> 7) & 1)
        val = (val << 1) & 0xFF; self._write(addr, val); self._set_zn(val)

    def _op_bcc(self, addr): self._branch(not (self.status & C_FLAG))
    def _op_bcs(self, addr): self._branch(bool(self.status & C_FLAG))
    def _op_beq(self, addr): self._branch(bool(self.status & Z_FLAG))

    def _op_bit(self, addr):
        val = self._read(addr)
        self.status = (self.status & ~(V_FLAG | N_FLAG)) | (val & (V_FLAG | N_FLAG))
        if (self.a & val) == 0: self.status |= Z_FLAG
        else: self.status &= ~Z_FLAG

    def _op_bmi(self, addr): self._branch(bool(self.status & N_FLAG))
    def _op_bne(self, addr): self._branch(not (self.status & Z_FLAG))
    def _op_bpl(self, addr): self._branch(not (self.status & N_FLAG))

    def _op_brk(self, addr):
        self.pc = (self.pc + 1) & 0xFFFF
        self._push16(self.pc)
        self._push(self.status | B_FLAG | U_FLAG)
        self.status |= I_FLAG
        self.pc = self._read16(0xFFFE)

    def _op_bvc(self, addr): self._branch(not (self.status & V_FLAG))
    def _op_bvs(self, addr): self._branch(bool(self.status & V_FLAG))
    def _op_clc(self, addr): self.status &= ~C_FLAG
    def _op_cld(self, addr): self.status &= ~D_FLAG
    def _op_cli(self, addr): self.status &= ~I_FLAG
    def _op_clv(self, addr): self.status &= ~V_FLAG

    def _op_cmp(self, addr):
        val = self._read(addr); diff = self.a - val
        self.status = (self.status & ~C_FLAG) | (C_FLAG if self.a >= val else 0)
        self._set_zn(diff & 0xFF)

    def _op_cpx(self, addr):
        val = self._read(addr); diff = self.x - val
        self.status = (self.status & ~C_FLAG) | (C_FLAG if self.x >= val else 0)
        self._set_zn(diff & 0xFF)

    def _op_cpy(self, addr):
        val = self._read(addr); diff = self.y - val
        self.status = (self.status & ~C_FLAG) | (C_FLAG if self.y >= val else 0)
        self._set_zn(diff & 0xFF)

    def _op_dec(self, addr):
        val = (self._read(addr) - 1) & 0xFF; self._write(addr, val); self._set_zn(val)

    def _op_dex(self, addr): self.x = (self.x - 1) & 0xFF; self._set_zn(self.x)
    def _op_dey(self, addr): self.y = (self.y - 1) & 0xFF; self._set_zn(self.y)

    def _op_eor(self, addr):
        self.a ^= self._read(addr); self._set_zn(self.a)

    def _op_inc(self, addr):
        val = (self._read(addr) + 1) & 0xFF; self._write(addr, val); self._set_zn(val)

    def _op_inx(self, addr): self.x = (self.x + 1) & 0xFF; self._set_zn(self.x)
    def _op_iny(self, addr): self.y = (self.y + 1) & 0xFF; self._set_zn(self.y)
    def _op_jmp(self, addr): self.pc = addr

    def _op_jsr(self, addr):
        self._push16((self.pc - 1) & 0xFFFF); self.pc = addr

    def _op_lda(self, addr): self.a = self._read(addr); self._set_zn(self.a)
    def _op_ldx(self, addr): self.x = self._read(addr); self._set_zn(self.x)
    def _op_ldy(self, addr): self.y = self._read(addr); self._set_zn(self.y)

    def _op_lsr_acc(self, addr):
        self.status = (self.status & ~C_FLAG) | (self.a & 1)
        self.a >>= 1; self._set_zn(self.a)

    def _op_lsr_mem(self, addr):
        val = self._read(addr)
        self.status = (self.status & ~C_FLAG) | (val & 1)
        val >>= 1; self._write(addr, val); self._set_zn(val)

    def _op_nop(self, addr): pass

    def _op_ora(self, addr):
        self.a |= self._read(addr); self._set_zn(self.a)

    def _op_pha(self, addr): self._push(self.a)
    def _op_php(self, addr): self._push(self.status | B_FLAG | U_FLAG)

    def _op_pla(self, addr):
        self.a = self._pull(); self._set_zn(self.a)

    def _op_plp(self, addr):
        self.status = (self._pull() & ~B_FLAG) | U_FLAG

    def _op_rol_acc(self, addr):
        c = 1 if (self.status & C_FLAG) else 0
        self.status = (self.status & ~C_FLAG) | ((self.a >> 7) & 1)
        self.a = ((self.a << 1) | c) & 0xFF; self._set_zn(self.a)

    def _op_rol_mem(self, addr):
        val = self._read(addr); c = 1 if (self.status & C_FLAG) else 0
        self.status = (self.status & ~C_FLAG) | ((val >> 7) & 1)
        val = ((val << 1) | c) & 0xFF; self._write(addr, val); self._set_zn(val)

    def _op_ror_acc(self, addr):
        c = 1 if (self.status & C_FLAG) else 0
        self.status = (self.status & ~C_FLAG) | (self.a & 1)
        self.a = ((self.a >> 1) | (c << 7)) & 0xFF; self._set_zn(self.a)

    def _op_ror_mem(self, addr):
        val = self._read(addr); c = 1 if (self.status & C_FLAG) else 0
        self.status = (self.status & ~C_FLAG) | (val & 1)
        val = ((val >> 1) | (c << 7)) & 0xFF; self._write(addr, val); self._set_zn(val)

    def _op_rti(self, addr):
        self.status = (self._pull() & ~B_FLAG) | U_FLAG; self.pc = self._pull16()

    def _op_rts(self, addr):
        self.pc = (self._pull16() + 1) & 0xFFFF

    def _op_sbc(self, addr):
        val = self._read(addr); a = self.a; c = 1 if (self.status & C_FLAG) else 0
        result = a - val - (1 - c)
        self.status &= ~(C_FLAG | V_FLAG)
        if result >= 0: self.status |= C_FLAG
        if ((a ^ result) & (~val ^ result) & 0x80): self.status |= V_FLAG
        self.a = result & 0xFF; self._set_zn(self.a)

    def _op_sec(self, addr): self.status |= C_FLAG
    def _op_sed(self, addr): self.status |= D_FLAG
    def _op_sei(self, addr): self.status |= I_FLAG
    def _op_sta(self, addr): self._write(addr, self.a)
    def _op_stx(self, addr): self._write(addr, self.x)
    def _op_sty(self, addr): self._write(addr, self.y)
    def _op_tax(self, addr): self.x = self.a; self._set_zn(self.x)
    def _op_tay(self, addr): self.y = self.a; self._set_zn(self.y)
    def _op_tsx(self, addr): self.x = self.sp; self._set_zn(self.x)
    def _op_txa(self, addr): self.a = self.x; self._set_zn(self.a)
    def _op_txs(self, addr): self.sp = self.x
    def _op_tya(self, addr): self.a = self.y; self._set_zn(self.a)

    # ── unofficial NOPs ──────────────────────────────────────────────────────
    def _op_dop(self, addr): self._read(addr)
    def _op_top(self, addr): self._read(addr)

    # ── unofficial combo opcodes ─────────────────────────────────────────────
    def _op_lax(self, addr):
        val = self._read(addr); self.a = val; self.x = val; self._set_zn(val)

    def _op_sax(self, addr):
        self._write(addr, self.a & self.x)

    def _op_dcp(self, addr):
        val = (self._read(addr) - 1) & 0xFF; self._write(addr, val)
        diff = self.a - val
        self.status = (self.status & ~C_FLAG) | (C_FLAG if self.a >= val else 0)
        self._set_zn(diff & 0xFF)

    def _op_isb(self, addr):
        val = (self._read(addr) + 1) & 0xFF; self._write(addr, val)
        a = self.a; c = 1 if (self.status & C_FLAG) else 0
        result = a - val - (1 - c)
        self.status &= ~(C_FLAG | V_FLAG)
        if result >= 0: self.status |= C_FLAG
        if ((a ^ result) & (~val ^ result) & 0x80): self.status |= V_FLAG
        self.a = result & 0xFF; self._set_zn(self.a)

    def _op_slo(self, addr):
        val = self._read(addr)
        self.status = (self.status & ~C_FLAG) | ((val >> 7) & 1)
        val = (val << 1) & 0xFF; self._write(addr, val)
        self.a |= val; self._set_zn(self.a)

    def _op_rla(self, addr):
        val = self._read(addr); c = 1 if (self.status & C_FLAG) else 0
        self.status = (self.status & ~C_FLAG) | ((val >> 7) & 1)
        val = ((val << 1) | c) & 0xFF; self._write(addr, val)
        self.a &= val; self._set_zn(self.a)

    def _op_sre(self, addr):
        val = self._read(addr)
        self.status = (self.status & ~C_FLAG) | (val & 1)
        val >>= 1; self._write(addr, val)
        self.a ^= val; self._set_zn(self.a)

    def _op_rra(self, addr):
        val = self._read(addr); c = 1 if (self.status & C_FLAG) else 0
        self.status = (self.status & ~C_FLAG) | (val & 1)
        val = ((val >> 1) | (c << 7)) & 0xFF; self._write(addr, val)
        a = self.a; c2 = 1 if (self.status & C_FLAG) else 0
        result = a + val + c2
        self.status &= ~(C_FLAG | V_FLAG)
        if result > 0xFF: self.status |= C_FLAG
        if ((a ^ result) & (val ^ result) & 0x80): self.status |= V_FLAG
        self.a = result & 0xFF; self._set_zn(self.a)

    # ── opcode dispatch table ────────────────────────────────────────────────
    def _build_opcode_table(self):
        nop_entry = (self._op_nop, self._am_imp, 2)
        T = [nop_entry] * 256
        self._opcode_table = T

        # ADC
        T[0x69]=(self._op_adc,self._am_imm,2); T[0x65]=(self._op_adc,self._am_zp,3)
        T[0x75]=(self._op_adc,self._am_zpx,4); T[0x6D]=(self._op_adc,self._am_abs,4)
        T[0x7D]=(self._op_adc,self._am_abx,4); T[0x79]=(self._op_adc,self._am_aby,4)
        T[0x61]=(self._op_adc,self._am_izx,6); T[0x71]=(self._op_adc,self._am_izy,5)
        # AND
        T[0x29]=(self._op_and,self._am_imm,2); T[0x25]=(self._op_and,self._am_zp,3)
        T[0x35]=(self._op_and,self._am_zpx,4); T[0x2D]=(self._op_and,self._am_abs,4)
        T[0x3D]=(self._op_and,self._am_abx,4); T[0x39]=(self._op_and,self._am_aby,4)
        T[0x21]=(self._op_and,self._am_izx,6); T[0x31]=(self._op_and,self._am_izy,5)
        # ASL
        T[0x0A]=(self._op_asl_acc,self._am_acc,2); T[0x06]=(self._op_asl_mem,self._am_zp,5)
        T[0x16]=(self._op_asl_mem,self._am_zpx,6); T[0x0E]=(self._op_asl_mem,self._am_abs,6)
        T[0x1E]=(self._op_asl_mem,self._am_abx_nopage,7)
        # Branches
        T[0x90]=(self._op_bcc,self._am_imp,2); T[0xB0]=(self._op_bcs,self._am_imp,2)
        T[0xF0]=(self._op_beq,self._am_imp,2); T[0x30]=(self._op_bmi,self._am_imp,2)
        T[0xD0]=(self._op_bne,self._am_imp,2); T[0x10]=(self._op_bpl,self._am_imp,2)
        T[0x50]=(self._op_bvc,self._am_imp,2); T[0x70]=(self._op_bvs,self._am_imp,2)
        # BIT
        T[0x24]=(self._op_bit,self._am_zp,3); T[0x2C]=(self._op_bit,self._am_abs,4)
        # BRK
        T[0x00]=(self._op_brk,self._am_imp,7)
        # Flags
        T[0x18]=(self._op_clc,self._am_imp,2); T[0xD8]=(self._op_cld,self._am_imp,2)
        T[0x58]=(self._op_cli,self._am_imp,2); T[0xB8]=(self._op_clv,self._am_imp,2)
        T[0x38]=(self._op_sec,self._am_imp,2); T[0xF8]=(self._op_sed,self._am_imp,2)
        T[0x78]=(self._op_sei,self._am_imp,2)
        # CMP
        T[0xC9]=(self._op_cmp,self._am_imm,2); T[0xC5]=(self._op_cmp,self._am_zp,3)
        T[0xD5]=(self._op_cmp,self._am_zpx,4); T[0xCD]=(self._op_cmp,self._am_abs,4)
        T[0xDD]=(self._op_cmp,self._am_abx,4); T[0xD9]=(self._op_cmp,self._am_aby,4)
        T[0xC1]=(self._op_cmp,self._am_izx,6); T[0xD1]=(self._op_cmp,self._am_izy,5)
        # CPX
        T[0xE0]=(self._op_cpx,self._am_imm,2); T[0xE4]=(self._op_cpx,self._am_zp,3)
        T[0xEC]=(self._op_cpx,self._am_abs,4)
        # CPY
        T[0xC0]=(self._op_cpy,self._am_imm,2); T[0xC4]=(self._op_cpy,self._am_zp,3)
        T[0xCC]=(self._op_cpy,self._am_abs,4)
        # DEC
        T[0xC6]=(self._op_dec,self._am_zp,5); T[0xD6]=(self._op_dec,self._am_zpx,6)
        T[0xCE]=(self._op_dec,self._am_abs,6); T[0xDE]=(self._op_dec,self._am_abx_nopage,7)
        # DEX DEY
        T[0xCA]=(self._op_dex,self._am_imp,2); T[0x88]=(self._op_dey,self._am_imp,2)
        # EOR
        T[0x49]=(self._op_eor,self._am_imm,2); T[0x45]=(self._op_eor,self._am_zp,3)
        T[0x55]=(self._op_eor,self._am_zpx,4); T[0x4D]=(self._op_eor,self._am_abs,4)
        T[0x5D]=(self._op_eor,self._am_abx,4); T[0x59]=(self._op_eor,self._am_aby,4)
        T[0x41]=(self._op_eor,self._am_izx,6); T[0x51]=(self._op_eor,self._am_izy,5)
        # INC
        T[0xE6]=(self._op_inc,self._am_zp,5); T[0xF6]=(self._op_inc,self._am_zpx,6)
        T[0xEE]=(self._op_inc,self._am_abs,6); T[0xFE]=(self._op_inc,self._am_abx_nopage,7)
        # INX INY
        T[0xE8]=(self._op_inx,self._am_imp,2); T[0xC8]=(self._op_iny,self._am_imp,2)
        # JMP
        T[0x4C]=(self._op_jmp,self._am_abs,3); T[0x6C]=(self._op_jmp,self._am_ind,5)
        # JSR
        T[0x20]=(self._op_jsr,self._am_abs,6)
        # LDA
        T[0xA9]=(self._op_lda,self._am_imm,2); T[0xA5]=(self._op_lda,self._am_zp,3)
        T[0xB5]=(self._op_lda,self._am_zpx,4); T[0xAD]=(self._op_lda,self._am_abs,4)
        T[0xBD]=(self._op_lda,self._am_abx,4); T[0xB9]=(self._op_lda,self._am_aby,4)
        T[0xA1]=(self._op_lda,self._am_izx,6); T[0xB1]=(self._op_lda,self._am_izy,5)
        # LDX
        T[0xA2]=(self._op_ldx,self._am_imm,2); T[0xA6]=(self._op_ldx,self._am_zp,3)
        T[0xB6]=(self._op_ldx,self._am_zpy,4); T[0xAE]=(self._op_ldx,self._am_abs,4)
        T[0xBE]=(self._op_ldx,self._am_aby,4)
        # LDY
        T[0xA0]=(self._op_ldy,self._am_imm,2); T[0xA4]=(self._op_ldy,self._am_zp,3)
        T[0xB4]=(self._op_ldy,self._am_zpx,4); T[0xAC]=(self._op_ldy,self._am_abs,4)
        T[0xBC]=(self._op_ldy,self._am_abx,4)
        # LSR
        T[0x4A]=(self._op_lsr_acc,self._am_acc,2); T[0x46]=(self._op_lsr_mem,self._am_zp,5)
        T[0x56]=(self._op_lsr_mem,self._am_zpx,6); T[0x4E]=(self._op_lsr_mem,self._am_abs,6)
        T[0x5E]=(self._op_lsr_mem,self._am_abx_nopage,7)
        # NOP
        T[0xEA]=(self._op_nop,self._am_imp,2)
        # ORA
        T[0x09]=(self._op_ora,self._am_imm,2); T[0x05]=(self._op_ora,self._am_zp,3)
        T[0x15]=(self._op_ora,self._am_zpx,4); T[0x0D]=(self._op_ora,self._am_abs,4)
        T[0x1D]=(self._op_ora,self._am_abx,4); T[0x19]=(self._op_ora,self._am_aby,4)
        T[0x01]=(self._op_ora,self._am_izx,6); T[0x11]=(self._op_ora,self._am_izy,5)
        # PHA PHP PLA PLP
        T[0x48]=(self._op_pha,self._am_imp,3); T[0x08]=(self._op_php,self._am_imp,3)
        T[0x68]=(self._op_pla,self._am_imp,4); T[0x28]=(self._op_plp,self._am_imp,4)
        # ROL
        T[0x2A]=(self._op_rol_acc,self._am_acc,2); T[0x26]=(self._op_rol_mem,self._am_zp,5)
        T[0x36]=(self._op_rol_mem,self._am_zpx,6); T[0x2E]=(self._op_rol_mem,self._am_abs,6)
        T[0x3E]=(self._op_rol_mem,self._am_abx_nopage,7)
        # ROR
        T[0x6A]=(self._op_ror_acc,self._am_acc,2); T[0x66]=(self._op_ror_mem,self._am_zp,5)
        T[0x76]=(self._op_ror_mem,self._am_zpx,6); T[0x6E]=(self._op_ror_mem,self._am_abs,6)
        T[0x7E]=(self._op_ror_mem,self._am_abx_nopage,7)
        # RTI RTS
        T[0x40]=(self._op_rti,self._am_imp,6); T[0x60]=(self._op_rts,self._am_imp,6)
        # SBC
        T[0xE9]=(self._op_sbc,self._am_imm,2); T[0xE5]=(self._op_sbc,self._am_zp,3)
        T[0xF5]=(self._op_sbc,self._am_zpx,4); T[0xED]=(self._op_sbc,self._am_abs,4)
        T[0xFD]=(self._op_sbc,self._am_abx,4); T[0xF9]=(self._op_sbc,self._am_aby,4)
        T[0xE1]=(self._op_sbc,self._am_izx,6); T[0xF1]=(self._op_sbc,self._am_izy,5)
        # STA
        T[0x85]=(self._op_sta,self._am_zp,3); T[0x95]=(self._op_sta,self._am_zpx,4)
        T[0x8D]=(self._op_sta,self._am_abs,4); T[0x9D]=(self._op_sta,self._am_abx_nopage,5)
        T[0x99]=(self._op_sta,self._am_aby_nopage,5); T[0x81]=(self._op_sta,self._am_izx,6)
        T[0x91]=(self._op_sta,self._am_izy_nopage,6)
        # STX
        T[0x86]=(self._op_stx,self._am_zp,3); T[0x96]=(self._op_stx,self._am_zpy,4)
        T[0x8E]=(self._op_stx,self._am_abs,4)
        # STY
        T[0x84]=(self._op_sty,self._am_zp,3); T[0x94]=(self._op_sty,self._am_zpx,4)
        T[0x8C]=(self._op_sty,self._am_abs,4)
        # Transfers
        T[0xAA]=(self._op_tax,self._am_imp,2); T[0xA8]=(self._op_tay,self._am_imp,2)
        T[0xBA]=(self._op_tsx,self._am_imp,2); T[0x8A]=(self._op_txa,self._am_imp,2)
        T[0x9A]=(self._op_txs,self._am_imp,2); T[0x98]=(self._op_tya,self._am_imp,2)

        # ── Unofficial opcodes ───────────────────────────────────────────────
        # LAX
        T[0xA7]=(self._op_lax,self._am_zp,3); T[0xB7]=(self._op_lax,self._am_zpy,4)
        T[0xAF]=(self._op_lax,self._am_abs,4); T[0xBF]=(self._op_lax,self._am_aby,4)
        T[0xA3]=(self._op_lax,self._am_izx,6); T[0xB3]=(self._op_lax,self._am_izy,5)
        # SAX
        T[0x87]=(self._op_sax,self._am_zp,3); T[0x97]=(self._op_sax,self._am_zpy,4)
        T[0x8F]=(self._op_sax,self._am_abs,4); T[0x83]=(self._op_sax,self._am_izx,6)
        # DCP
        T[0xC7]=(self._op_dcp,self._am_zp,5); T[0xD7]=(self._op_dcp,self._am_zpx,6)
        T[0xCF]=(self._op_dcp,self._am_abs,6); T[0xDF]=(self._op_dcp,self._am_abx_nopage,7)
        T[0xDB]=(self._op_dcp,self._am_aby_nopage,7); T[0xC3]=(self._op_dcp,self._am_izx,8)
        T[0xD3]=(self._op_dcp,self._am_izy_nopage,8)
        # ISB
        T[0xE7]=(self._op_isb,self._am_zp,5); T[0xF7]=(self._op_isb,self._am_zpx,6)
        T[0xEF]=(self._op_isb,self._am_abs,6); T[0xFF]=(self._op_isb,self._am_abx_nopage,7)
        T[0xFB]=(self._op_isb,self._am_aby_nopage,7); T[0xE3]=(self._op_isb,self._am_izx,8)
        T[0xF3]=(self._op_isb,self._am_izy_nopage,8)
        # SLO
        T[0x07]=(self._op_slo,self._am_zp,5); T[0x17]=(self._op_slo,self._am_zpx,6)
        T[0x0F]=(self._op_slo,self._am_abs,6); T[0x1F]=(self._op_slo,self._am_abx_nopage,7)
        T[0x1B]=(self._op_slo,self._am_aby_nopage,7); T[0x03]=(self._op_slo,self._am_izx,8)
        T[0x13]=(self._op_slo,self._am_izy_nopage,8)
        # RLA
        T[0x27]=(self._op_rla,self._am_zp,5); T[0x37]=(self._op_rla,self._am_zpx,6)
        T[0x2F]=(self._op_rla,self._am_abs,6); T[0x3F]=(self._op_rla,self._am_abx_nopage,7)
        T[0x3B]=(self._op_rla,self._am_aby_nopage,7); T[0x23]=(self._op_rla,self._am_izx,8)
        T[0x33]=(self._op_rla,self._am_izy_nopage,8)
        # SRE
        T[0x47]=(self._op_sre,self._am_zp,5); T[0x57]=(self._op_sre,self._am_zpx,6)
        T[0x4F]=(self._op_sre,self._am_abs,6); T[0x5F]=(self._op_sre,self._am_abx_nopage,7)
        T[0x5B]=(self._op_sre,self._am_aby_nopage,7); T[0x43]=(self._op_sre,self._am_izx,8)
        T[0x53]=(self._op_sre,self._am_izy_nopage,8)
        # RRA
        T[0x67]=(self._op_rra,self._am_zp,5); T[0x77]=(self._op_rra,self._am_zpx,6)
        T[0x6F]=(self._op_rra,self._am_abs,6); T[0x7F]=(self._op_rra,self._am_abx_nopage,7)
        T[0x7B]=(self._op_rra,self._am_aby_nopage,7); T[0x63]=(self._op_rra,self._am_izx,8)
        T[0x73]=(self._op_rra,self._am_izy_nopage,8)
        # Unofficial SBC mirror
        T[0xEB]=(self._op_sbc,self._am_imm,2)
        # Unofficial NOPs
        for op in [0x1A,0x3A,0x5A,0x7A,0xDA,0xFA]:
            T[op]=(self._op_nop,self._am_imp,2)
        for op in [0x04,0x14,0x34,0x44,0x54,0x64,0x74,0x80,0x82,0x89,0xC2,0xD4,0xE2,0xF4]:
            T[op]=(self._op_dop,self._am_imm,2)
        for op in [0x0C,0x1C,0x3C,0x5C,0x7C,0xDC,0xFC]:
            T[op]=(self._op_top,self._am_abx,4)

    # ── reset / interrupts / step ────────────────────────────────────────────
    def reset(self):
        lo = self._read(0xFFFC); hi = self._read(0xFFFD)
        self.pc = (hi << 8) | lo; self.sp = 0xFD; self.status = 0x24; self.cycles = 0

    def nmi(self):
        self._push16(self.pc); self._push(self.status | U_FLAG)
        self.status |= I_FLAG; self.pc = self._read16(0xFFFA); self.cycles += 7

    def irq(self):
        if not (self.status & I_FLAG):
            self._push16(self.pc); self._push(self.status | U_FLAG)
            self.status |= I_FLAG; self.pc = self._read16(0xFFFE); self.cycles += 7

    def step(self):
        if self.nmi_pending:
            self.nmi_pending = False; self.nmi(); return 7
        if self.irq_pending and not (self.status & I_FLAG):
            self.irq_pending = False; self.irq(); return 7
        opcode = self._read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        handler, addr_mode, base_cycles = self._opcode_table[opcode]
        self.cycles = 0; addr = addr_mode(); handler(addr)
        total = base_cycles + self.cycles; self.total_cycles += total
        return total


# ═════════════════════════════════════════════════════════════════════════════
#  RP2C02 PPU
# ═════════════════════════════════════════════════════════════════════════════

class PPU:
    """NES Picture Processing Unit – scanline-accurate rendering."""

    def __init__(self, nes):
        self.nes = nes
        self.nametable = bytearray(2048)
        self.palette_ram = bytearray(32)
        self.oam = bytearray(256)
        self.chr_rom = bytearray(8192)
        self.chr_ram = False

        self.ctrl = 0
        self.mask = 0
        self.oam_addr = 0
        self.v = 0
        self.t = 0
        self.fine_x = 0
        self.w = 0
        self.data_buf = 0

        self.sprite_zero_hit = False
        self.sprite_overflow = False
        self.nmi_occurred = False
        self.nmi_output = False

        self.scanline = -1
        self.dot = 0
        self.frame_count = 0
        self.frame_complete = False

        self._nt_byte = 0
        self._at_byte = 0
        self._tile_lo = 0
        self._tile_hi = 0
        self._bg_shift_lo = 0
        self._bg_shift_hi = 0
        self._at_shift_lo = 0
        self._at_shift_hi = 0
        self._at_latch_lo = 0
        self._at_latch_hi = 0

        self._sprite_count = 0
        self._sprite_patterns_lo = [0] * 8
        self._sprite_patterns_hi = [0] * 8
        self._sprite_positions = [0] * 8
        self._sprite_priorities = [0] * 8
        self._sprite_indices = [0] * 8

        self.framebuffer = np.zeros((240, 256, 3), dtype=np.uint8)

    # ── VRAM access ──────────────────────────────────────────────────────────
    def ppu_read(self, addr):
        addr &= 0x3FFF
        if addr < 0x2000:
            return self.nes._ppu_chr_read_dispatch(addr)
        elif addr < 0x3F00:
            return self.nametable[self._mirror_nt(addr)]
        else:
            return self._pal_read(addr)

    def ppu_write(self, addr, val):
        addr &= 0x3FFF; val &= 0xFF
        if addr < 0x2000:
            if self.chr_ram:
                self.chr_rom[addr % len(self.chr_rom)] = val
        elif addr < 0x3F00:
            self.nametable[self._mirror_nt(addr)] = val
        else:
            self._pal_write(addr, val)

    def _mirror_nt(self, addr):
        addr = (addr - 0x2000) & 0x0FFF
        if self.nes.mirroring == 1:  # vertical
            return addr & 0x07FF
        else:  # horizontal
            return addr & 0x03FF if addr < 0x0800 else 0x0400 + (addr & 0x03FF)

    def _pal_read(self, addr):
        idx = addr & 0x1F
        if idx in (0x10, 0x14, 0x18, 0x1C): idx -= 0x10
        return self.palette_ram[idx]

    def _pal_write(self, addr, val):
        idx = addr & 0x1F
        if idx in (0x10, 0x14, 0x18, 0x1C): idx -= 0x10
        self.palette_ram[idx] = val

    # ── Register I/O ─────────────────────────────────────────────────────────
    def write_ctrl(self, val):
        self.ctrl = val
        old_nmi = self.nmi_output
        self.nmi_output = bool(val & 0x80)
        self.t = (self.t & 0x73FF) | ((val & 0x03) << 10)
        if not old_nmi and self.nmi_output and self.nmi_occurred:
            self.nes.cpu.nmi_pending = True

    def write_mask(self, val): self.mask = val

    def read_status(self):
        r = (0x80 if self.nmi_occurred else 0) | \
            (0x40 if self.sprite_zero_hit else 0) | \
            (0x20 if self.sprite_overflow else 0)
        self.nmi_occurred = False; self.w = 0; return r

    def write_oam_addr(self, val): self.oam_addr = val
    def write_oam_data(self, val): self.oam[self.oam_addr] = val; self.oam_addr = (self.oam_addr + 1) & 0xFF
    def read_oam_data(self): return self.oam[self.oam_addr]

    def write_scroll(self, val):
        if self.w == 0:
            self.t = (self.t & 0x7FE0) | (val >> 3); self.fine_x = val & 0x07; self.w = 1
        else:
            self.t = (self.t & 0x0C1F) | ((val & 0x07) << 12) | ((val & 0xF8) << 2); self.w = 0

    def write_addr(self, val):
        if self.w == 0:
            self.t = (self.t & 0x00FF) | ((val & 0x3F) << 8); self.w = 1
        else:
            self.t = (self.t & 0xFF00) | val; self.v = self.t; self.w = 0

    def read_data(self):
        data = self.ppu_read(self.v)
        if (self.v & 0x3FFF) < 0x3F00:
            result = self.data_buf; self.data_buf = data
        else:
            result = data; self.data_buf = self.ppu_read(self.v - 0x1000)
        self.v = (self.v + (32 if (self.ctrl & 0x04) else 1)) & 0x7FFF
        return result

    def write_data(self, val):
        self.ppu_write(self.v, val)
        self.v = (self.v + (32 if (self.ctrl & 0x04) else 1)) & 0x7FFF

    # ── Rendering internals ──────────────────────────────────────────────────
    def _rendering_enabled(self):
        return (self.mask & 0x18) != 0

    def _inc_x(self):
        if (self.v & 0x001F) == 31: self.v = (self.v & ~0x001F) ^ 0x0400
        else: self.v += 1

    def _inc_y(self):
        if (self.v & 0x7000) != 0x7000: self.v += 0x1000
        else:
            self.v &= ~0x7000; y = (self.v & 0x03E0) >> 5
            if y == 29: y = 0; self.v ^= 0x0800
            elif y == 31: y = 0
            else: y += 1
            self.v = (self.v & ~0x03E0) | (y << 5)

    def _copy_x(self): self.v = (self.v & 0x7BE0) | (self.t & 0x041F)
    def _copy_y(self): self.v = (self.v & 0x041F) | (self.t & 0x7BE0)

    def _fetch_nt(self):
        self._nt_byte = self.ppu_read(0x2000 | (self.v & 0x0FFF))

    def _fetch_at(self):
        addr = 0x23C0 | (self.v & 0x0C00) | ((self.v >> 4) & 0x38) | ((self.v >> 2) & 0x07)
        at = self.ppu_read(addr)
        shift = ((self.v >> 4) & 4) | (self.v & 2)
        self._at_byte = (at >> shift) & 3

    def _fetch_tile_lo(self):
        table = 0x1000 if (self.ctrl & 0x10) else 0
        self._tile_lo = self.ppu_read(table + self._nt_byte * 16 + ((self.v >> 12) & 7))

    def _fetch_tile_hi(self):
        table = 0x1000 if (self.ctrl & 0x10) else 0
        self._tile_hi = self.ppu_read(table + self._nt_byte * 16 + ((self.v >> 12) & 7) + 8)

    def _load_bg_shift(self):
        self._bg_shift_lo = (self._bg_shift_lo & 0xFF00) | self._tile_lo
        self._bg_shift_hi = (self._bg_shift_hi & 0xFF00) | self._tile_hi
        self._at_latch_lo = 0xFF if (self._at_byte & 1) else 0
        self._at_latch_hi = 0xFF if (self._at_byte & 2) else 0

    def _update_shift(self):
        if self.mask & 0x08:
            self._bg_shift_lo <<= 1; self._bg_shift_hi <<= 1
            self._at_shift_lo = (self._at_shift_lo << 1) | (1 if self._at_latch_lo else 0)
            self._at_shift_hi = (self._at_shift_hi << 1) | (1 if self._at_latch_hi else 0)

    # ── Sprite evaluation ────────────────────────────────────────────────────
    def _eval_sprites(self):
        h = 16 if (self.ctrl & 0x20) else 8
        count = 0
        for i in range(64):
            sy = self.oam[i * 4]; diff = self.scanline - sy
            if 0 <= diff < h and count < 8:
                tile = self.oam[i * 4 + 1]; attr = self.oam[i * 4 + 2]; sx = self.oam[i * 4 + 3]
                flip_v = bool(attr & 0x80); flip_h = bool(attr & 0x40)
                priority = (attr >> 5) & 1

                if self.ctrl & 0x20:  # 8x16
                    table = (tile & 1) * 0x1000; tile_idx = tile & 0xFE
                    row = diff
                    if flip_v: row = 15 - row
                    if row >= 8: tile_idx += 1; row -= 8
                    addr = table + tile_idx * 16 + row
                else:
                    table = 0x1000 if (self.ctrl & 0x08) else 0
                    row = diff
                    if flip_v: row = 7 - row
                    addr = table + tile * 16 + row

                lo = self.ppu_read(addr); hi = self.ppu_read(addr + 8)
                if flip_h: lo = self._rev(lo); hi = self._rev(hi)

                self._sprite_patterns_lo[count] = lo
                self._sprite_patterns_hi[count] = hi
                self._sprite_positions[count] = sx
                self._sprite_priorities[count] = priority
                self._sprite_indices[count] = i
                count += 1
            if count >= 8:
                if i < 63: self.sprite_overflow = True
                break
        self._sprite_count = count

    @staticmethod
    def _rev(b):
        b = ((b & 0xF0) >> 4) | ((b & 0x0F) << 4)
        b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2)
        b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1)
        return b

    # ── Pixel output ─────────────────────────────────────────────────────────
    def _render_pixel(self):
        x = self.dot - 1; y = self.scanline
        bg_pixel = 0; bg_palette = 0
        if self.mask & 0x08:
            if (self.mask & 0x02) or x >= 8:
                bit = 0x8000 >> self.fine_x
                p0 = 1 if (self._bg_shift_lo & bit) else 0
                p1 = 1 if (self._bg_shift_hi & bit) else 0
                bg_pixel = (p1 << 1) | p0
                a0 = 1 if (self._at_shift_lo & bit) else 0
                a1 = 1 if (self._at_shift_hi & bit) else 0
                bg_palette = (a1 << 1) | a0

        spr_pixel = 0; spr_palette = 0; spr_priority = 0; spr_zero = False
        if self.mask & 0x10:
            if (self.mask & 0x04) or x >= 8:
                for i in range(self._sprite_count):
                    off = x - self._sprite_positions[i]
                    if 0 <= off < 8:
                        bit = 7 - off
                        p0 = (self._sprite_patterns_lo[i] >> bit) & 1
                        p1 = (self._sprite_patterns_hi[i] >> bit) & 1
                        pixel = (p1 << 1) | p0
                        if pixel != 0:
                            spr_pixel = pixel
                            spr_palette = (self.oam[self._sprite_indices[i] * 4 + 2] & 0x03) + 4
                            spr_priority = self._sprite_priorities[i]
                            spr_zero = (self._sprite_indices[i] == 0)
                            break

        final_pixel = 0; final_palette = 0
        if bg_pixel == 0 and spr_pixel == 0: pass
        elif bg_pixel == 0: final_pixel = spr_pixel; final_palette = spr_palette
        elif spr_pixel == 0: final_pixel = bg_pixel; final_palette = bg_palette
        else:
            if spr_zero and x < 255: self.sprite_zero_hit = True
            if spr_priority == 0: final_pixel = spr_pixel; final_palette = spr_palette
            else: final_pixel = bg_pixel; final_palette = bg_palette

        ci = self.ppu_read(0x3F00 + final_palette * 4 + final_pixel) & 0x3F
        r, g, b = NES_PALETTE[ci]
        if 0 <= y < 240 and 0 <= x < 256:
            self.framebuffer[y, x, 0] = r
            self.framebuffer[y, x, 1] = g
            self.framebuffer[y, x, 2] = b

    # ── Main tick ────────────────────────────────────────────────────────────
    def tick(self):
        rendering = self._rendering_enabled()

        if self.scanline == -1:
            if self.dot == 1:
                self.nmi_occurred = False; self.sprite_zero_hit = False
                self.sprite_overflow = False; self.frame_complete = False
            if rendering:
                if 280 <= self.dot <= 304: self._copy_y()
                if self.dot == 257: self._copy_x()
                if (2 <= self.dot <= 257) or (322 <= self.dot <= 337):
                    self._update_shift()
                    phase = (self.dot - 1) % 8
                    if phase == 0: self._load_bg_shift(); self._fetch_nt()
                    elif phase == 2: self._fetch_at()
                    elif phase == 4: self._fetch_tile_lo()
                    elif phase == 6: self._fetch_tile_hi()
                    elif phase == 7: self._inc_x()
                if self.dot == 256: self._inc_y()
                if self.dot == 257: self._load_bg_shift(); self._copy_x()

        elif 0 <= self.scanline <= 239:
            if rendering:
                if self.dot == 257: self._eval_sprites()
                if (2 <= self.dot <= 257) or (322 <= self.dot <= 337):
                    self._update_shift()
                    phase = (self.dot - 1) % 8
                    if phase == 0: self._load_bg_shift(); self._fetch_nt()
                    elif phase == 2: self._fetch_at()
                    elif phase == 4: self._fetch_tile_lo()
                    elif phase == 6: self._fetch_tile_hi()
                    elif phase == 7: self._inc_x()
                if self.dot == 256: self._inc_y()
                if self.dot == 257: self._load_bg_shift(); self._copy_x()
            if 1 <= self.dot <= 256: self._render_pixel()

        elif self.scanline == 241 and self.dot == 1:
            self.nmi_occurred = True
            if self.nmi_output: self.nes.cpu.nmi_pending = True
            self.frame_complete = True

        self.dot += 1
        if self.dot > 340:
            self.dot = 0; self.scanline += 1
            if self.scanline > 260:
                self.scanline = -1; self.frame_count += 1
                if rendering and (self.frame_count & 1): self.dot = 1


# ═════════════════════════════════════════════════════════════════════════════
#  NES SYSTEM BUS  (CPU ↔ PPU ↔ Mappers ↔ Controllers)
# ═════════════════════════════════════════════════════════════════════════════

class NES:
    """Top-level NES system."""

    def __init__(self):
        self.ram = bytearray(2048)
        self.prg_rom = bytearray()
        self.prg_ram = bytearray(8192)
        self.mapper = 0
        self.mirroring = 0  # 0=H 1=V

        self.ppu = PPU(self)
        self.cpu = CPU(self)

        self.controller = [0, 0]
        self._ctrl_shift = [0, 0]
        self._ctrl_strobe = False

        self.framebuffer = self.ppu.framebuffer

        self._prg_bank_count = 0
        self._chr_bank_count = 0
        # MMC1
        self._m1_shift = 0; self._m1_count = 0; self._m1_ctrl = 0x0C
        self._m1_chr0 = 0; self._m1_chr1 = 0; self._m1_prg = 0
        # UxROM
        self._ux_bank = 0
        # CNROM
        self._cn_bank = 0

    def load_rom(self, prg, chr_data):
        self.prg_rom = bytearray(prg)
        self._prg_bank_count = max(1, len(self.prg_rom) // 16384)
        if chr_data and len(chr_data) > 0:
            self.ppu.chr_rom = bytearray(chr_data)
            self.ppu.chr_ram = False
            self._chr_bank_count = max(1, len(chr_data) // 8192)
        else:
            self.ppu.chr_rom = bytearray(8192)
            self.ppu.chr_ram = True
            self._chr_bank_count = 1
        self._m1_shift = 0; self._m1_count = 0; self._m1_ctrl = 0x0C
        self._m1_chr0 = 0; self._m1_chr1 = 0; self._m1_prg = 0
        self._ux_bank = 0; self._cn_bank = 0
        self.cpu.reset()
        self.ppu.scanline = -1; self.ppu.dot = 0
        self.ppu.frame_count = 0; self.ppu.frame_complete = False

    def set_mapper(self, m): self.mapper = m
    def set_mirroring(self, m): self.mirroring = m

    # ── CPU bus ──────────────────────────────────────────────────────────────
    def cpu_read(self, addr):
        addr &= 0xFFFF
        if addr < 0x2000: return self.ram[addr & 0x07FF]
        elif addr < 0x4000: return self._ppu_read(addr)
        elif addr == 0x4016: return self._ctrl_read(0)
        elif addr == 0x4017: return self._ctrl_read(1)
        elif addr < 0x4020: return 0
        elif addr < 0x6000: return 0
        elif addr < 0x8000: return self.prg_ram[addr & 0x1FFF]
        else: return self._mapper_read(addr)

    def cpu_write(self, addr, val):
        addr &= 0xFFFF; val &= 0xFF
        if addr < 0x2000: self.ram[addr & 0x07FF] = val
        elif addr < 0x4000: self._ppu_write(addr, val)
        elif addr == 0x4014: self._oam_dma(val)
        elif addr == 0x4016: self._ctrl_strobe_w(val)
        elif addr < 0x4020: pass
        elif addr < 0x6000: pass
        elif addr < 0x8000: self.prg_ram[addr & 0x1FFF] = val
        else: self._mapper_write(addr, val)

    def _ppu_read(self, addr):
        r = addr & 7
        if r == 2: return self.ppu.read_status()
        elif r == 4: return self.ppu.read_oam_data()
        elif r == 7: return self.ppu.read_data()
        return 0

    def _ppu_write(self, addr, val):
        r = addr & 7
        if r == 0: self.ppu.write_ctrl(val)
        elif r == 1: self.ppu.write_mask(val)
        elif r == 3: self.ppu.write_oam_addr(val)
        elif r == 4: self.ppu.write_oam_data(val)
        elif r == 5: self.ppu.write_scroll(val)
        elif r == 6: self.ppu.write_addr(val)
        elif r == 7: self.ppu.write_data(val)

    def _oam_dma(self, page):
        base = page << 8
        for i in range(256):
            self.ppu.oam[(self.ppu.oam_addr + i) & 0xFF] = self.cpu_read(base + i)
        self.cpu.total_cycles += 513

    # ── Controllers ──────────────────────────────────────────────────────────
    def _ctrl_strobe_w(self, val):
        if val & 1:
            self._ctrl_strobe = True
            self._ctrl_shift[0] = self.controller[0]; self._ctrl_shift[1] = self.controller[1]
        else:
            if self._ctrl_strobe:
                self._ctrl_shift[0] = self.controller[0]; self._ctrl_shift[1] = self.controller[1]
            self._ctrl_strobe = False

    def _ctrl_read(self, port):
        if self._ctrl_strobe: return self.controller[port] & 1
        val = self._ctrl_shift[port] & 1; self._ctrl_shift[port] >>= 1; return val | 0x40

    def controller_write(self, port, v):
        if 0 <= port <= 1: self.controller[port] = v & 0xFF
    def set_controller_state(self, port, s):
        if 0 <= port <= 1: self.controller[port] = s & 0xFF
    def controller_strobe_write(self, val): self._ctrl_strobe_w(val)

    # ── CHR read dispatch (for mapper banking) ───────────────────────────────
    def _ppu_chr_read_dispatch(self, addr):
        """Called by PPU for all CHR reads < $2000."""
        if self.mapper == 3 and not self.ppu.chr_ram:
            offset = self._cn_bank * 8192 + (addr & 0x1FFF)
            return self.ppu.chr_rom[offset % len(self.ppu.chr_rom)] if self.ppu.chr_rom else 0
        elif self.mapper == 1 and not self.ppu.chr_ram:
            chr_mode = (self._m1_ctrl >> 4) & 1
            if chr_mode == 0:
                bank = (self._m1_chr0 & 0x1E) >> 1
                offset = bank * 8192 + (addr & 0x1FFF)
            else:
                if addr < 0x1000:
                    offset = self._m1_chr0 * 4096 + (addr & 0x0FFF)
                else:
                    offset = self._m1_chr1 * 4096 + (addr & 0x0FFF)
            return self.ppu.chr_rom[offset % len(self.ppu.chr_rom)] if self.ppu.chr_rom else 0
        else:
            return self.ppu.chr_rom[addr % len(self.ppu.chr_rom)] if self.ppu.chr_rom else 0

    # ── Mapper PRG ───────────────────────────────────────────────────────────
    def _mapper_read(self, addr):
        prg = self.prg_rom
        if not prg: return 0
        plen = len(prg)

        if self.mapper == 0:
            return prg[(addr - 0x8000) & (plen - 1)]

        elif self.mapper == 1:
            pm = (self._m1_ctrl >> 2) & 3; bank = self._m1_prg & 0x0F
            if pm <= 1:
                bank &= 0x0E; offset = bank * 16384 + (addr - 0x8000)
            elif pm == 2:
                offset = (addr - 0x8000) if addr < 0xC000 else bank * 16384 + (addr - 0xC000)
            else:
                offset = bank * 16384 + (addr - 0x8000) if addr < 0xC000 else \
                         (self._prg_bank_count - 1) * 16384 + (addr - 0xC000)
            return prg[offset % plen]

        elif self.mapper == 2:
            if addr < 0xC000: offset = self._ux_bank * 16384 + (addr - 0x8000)
            else: offset = (self._prg_bank_count - 1) * 16384 + (addr - 0xC000)
            return prg[offset % plen]

        elif self.mapper == 3:
            return prg[(addr - 0x8000) & (plen - 1)]

        else:
            return prg[(addr - 0x8000) % plen]

    def _mapper_write(self, addr, val):
        if self.mapper == 1: self._mmc1_write(addr, val)
        elif self.mapper == 2: self._ux_bank = val & 0x0F
        elif self.mapper == 3: self._cn_bank = val & 0x03

    def _mmc1_write(self, addr, val):
        if val & 0x80:
            self._m1_shift = 0; self._m1_count = 0; self._m1_ctrl |= 0x0C; return
        self._m1_shift |= ((val & 1) << self._m1_count); self._m1_count += 1
        if self._m1_count == 5:
            reg = (addr >> 13) & 3
            if reg == 0:
                self._m1_ctrl = self._m1_shift
                mm = self._m1_ctrl & 3
                if mm == 2: self.mirroring = 1
                elif mm == 3: self.mirroring = 0
            elif reg == 1: self._m1_chr0 = self._m1_shift
            elif reg == 2: self._m1_chr1 = self._m1_shift
            elif reg == 3: self._m1_prg = self._m1_shift
            self._m1_shift = 0; self._m1_count = 0

    # ── Frame step ───────────────────────────────────────────────────────────
    def step_frame(self):
        self.ppu.frame_complete = False
        safety = 0; max_ticks = 89342 * 2
        while not self.ppu.frame_complete and safety < max_ticks:
            cpu_cycles = self.cpu.step()
            for _ in range(cpu_cycles * 3):
                self.ppu.tick()
                if self.ppu.frame_complete: break
            safety += cpu_cycles * 3

    def get_framebuffer(self):
        return self.ppu.framebuffer


# ═════════════════════════════════════════════════════════════════════════════
#  TKINTER GUI  (only loaded when tkinter is available)
# ═════════════════════════════════════════════════════════════════════════════

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from PIL import Image, ImageTk
    import threading
    _HAS_GUI = True
except ImportError:
    _HAS_GUI = False

class NESEmulator:
    FRAME_TIME = 1.0 / 60.0

    def __init__(self):
        self.window = tk.Tk()
        self.window.title("AC NES Emu 0.1")
        self.window.geometry("540x530")
        self.window.resizable(False, False)
        self.window.configure(bg="#1e1e2e")

        self.nes = NES()

        self.canvas = tk.Canvas(self.window, width=512, height=480, bg="black",
                                highlightthickness=0)
        self.canvas.pack(pady=4)
        self.photo = None
        self.canvas_image_id = None

        self.key_map = {
            "z": 0x01, "x": 0x02, "a": 0x04, "s": 0x08,
            "Up": 0x10, "Down": 0x20, "Left": 0x40, "Right": 0x80,
        }
        self.controller_state = 0
        self.mapper = 0
        self.black_frame_count = 0

        # Menu
        menubar = tk.Menu(self.window)
        fm = tk.Menu(menubar, tearoff=False)
        fm.add_command(label="Load ROM...", command=self.load_rom)
        fm.add_separator()
        fm.add_command(label="Pause / Resume  (P)", command=self.toggle_pause)
        fm.add_command(label="Reset  (R)", command=self.reset_rom)
        fm.add_command(label="Debugger", command=self.show_debugger)
        fm.add_separator()
        fm.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=fm)
        self.window.config(menu=menubar)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_var.set("No ROM loaded")
        tk.Label(self.window, textvariable=self.status_var, bd=1, relief=tk.SUNKEN,
                 anchor=tk.W, bg="#2a2a2e", fg="#ffffff",
                 font=("Courier", 9)).pack(side=tk.BOTTOM, fill=tk.X)

        self.running = True
        self.paused = False
        self.emulation_thread = None
        self.rom_loaded = False
        self._rom_prg = None
        self._rom_chr = None
        self._fps_n = 0
        self._fps_t = time.time()

        self.window.protocol("WM_DELETE_WINDOW", self.on_close)
        self._bind_keys()

    def load_rom(self):
        path = filedialog.askopenfilename(
            title="Select NES ROM",
            filetypes=[("NES ROMs", "*.nes"), ("All", "*.*")])
        if not path: return
        try:
            data = open(path, "rb").read()
            if len(data) < 16 or data[:4] != b'NES\x1a':
                raise ValueError("Not a valid iNES ROM")
            f6 = data[6]; f7 = data[7]
            prg_size = data[4] * 16384; chr_size = data[5] * 8192
            mapper = (f6 >> 4) | (f7 & 0xF0)
            trainer = 512 if (f6 & 0x04) else 0
            mirror = "vertical" if (f6 & 0x01) else "horizontal"
            battery = bool(f6 & 0x02)
            if (f7 & 0x0C) == 0x08:
                raise ValueError("NES 2.0 not yet supported")
            self.mapper = mapper
            off = 16 + trainer
            if off + prg_size > len(data):
                raise ValueError("ROM file truncated")
            prg = data[off:off + prg_size]
            chr_d = data[off + prg_size:off + prg_size + chr_size] if chr_size > 0 else b''
            self._rom_prg = prg; self._rom_chr = chr_d
            self.nes.load_rom(prg, chr_d)
            self.nes.set_mapper(mapper)
            self.nes.set_mirroring(1 if mirror == "vertical" else 0)
            if mapper not in (0, 1, 2, 3):
                messagebox.showwarning("Mapper",
                    f"Mapper {mapper} not fully supported.\nBest with 0/1/2/3.")
            self.rom_loaded = True; self.black_frame_count = 0
            names = {0: "NROM", 1: "MMC1", 2: "UxROM", 3: "CNROM"}
            self.status_var.set(
                f"{os.path.basename(path)} | {names.get(mapper, f'm{mapper}')} | {mirror}"
                + (" | bat" if battery else ""))
            if not self.emulation_thread or not self.emulation_thread.is_alive():
                self.running = True; self.paused = False
                self.emulation_thread = threading.Thread(target=self._emu_loop, daemon=True)
                self.emulation_thread.start()
        except Exception as e:
            messagebox.showerror("Error", f"ROM load failed:\n{e}")
            traceback.print_exc()

    def reset_rom(self, event=None):
        if self._rom_prg is not None:
            self.nes.load_rom(self._rom_prg, self._rom_chr or b'')
            self.nes.set_mapper(self.mapper)
            self.black_frame_count = 0

    def _emu_loop(self):
        while self.running and self.rom_loaded:
            t0 = time.perf_counter()
            if not self.paused:
                try:
                    self.nes.step_frame()
                    fb = self.nes.get_framebuffer()
                    img = Image.fromarray(fb, "RGB").resize((512, 480), Image.NEAREST)
                    self.photo = ImageTk.PhotoImage(img)
                    try:
                        if not self.window.winfo_exists(): break
                        if self.canvas_image_id is None:
                            self.canvas_image_id = self.canvas.create_image(
                                0, 0, image=self.photo, anchor=tk.NW)
                        else:
                            self.canvas.itemconfig(self.canvas_image_id, image=self.photo)
                        self.canvas.update_idletasks()
                    except tk.TclError: break
                    self._fps_n += 1
                    now = time.time()
                    if now - self._fps_t >= 2.0:
                        fps = self._fps_n / (now - self._fps_t)
                        self._fps_n = 0; self._fps_t = now
                        base = self.status_var.get().split(" | fps")[0]
                        self.status_var.set(f"{base} | fps: {fps:.1f}")
                except Exception as e:
                    self.status_var.set(f"Error: {e}"); time.sleep(0.016)
            dt = time.perf_counter() - t0
            sl = self.FRAME_TIME - dt
            if sl > 0: time.sleep(sl)

    def _bind_keys(self):
        for key, val in self.key_map.items():
            self.window.bind(f"<KeyPress-{key}>", lambda e, v=val: self._kd(v))
            self.window.bind(f"<KeyRelease-{key}>", lambda e, v=val: self._ku(v))
        self.window.bind("<p>", self.toggle_pause)
        self.window.bind("<r>", self.reset_rom)

    def _kd(self, b):
        self.controller_state |= b; self._push_ctrl()
    def _ku(self, b):
        self.controller_state &= (~b) & 0xFF; self._push_ctrl()
    def _push_ctrl(self):
        self.nes.set_controller_state(0, self.controller_state)
        self.nes.controller_strobe_write(1); self.nes.controller_strobe_write(0)

    def toggle_pause(self, event=None):
        self.paused = not self.paused
        s = self.status_var.get()
        if self.paused: self.status_var.set(s.split(" | PAUSED")[0] + " | PAUSED")
        else: self.status_var.set(s.replace(" | PAUSED", ""))

    def show_debugger(self):
        d = tk.Toplevel(self.window); d.title("Debugger"); d.geometry("400x340")
        d.configure(bg="#1e1e2e")
        cpu = self.nes.cpu; ppu = self.nes.ppu
        p = cpu.status
        flags = f"{'N' if p&0x80 else '.'}{'V' if p&0x40 else '.'}-" \
                f"{'B' if p&0x10 else '.'}{'D' if p&0x08 else '.'}{'I' if p&0x04 else '.'}" \
                f"{'Z' if p&0x02 else '.'}{'C' if p&0x01 else '.'}"
        lines = [
            f"  A={cpu.a:02X}  X={cpu.x:02X}  Y={cpu.y:02X}  SP={cpu.sp:02X}",
            f"  PC={cpu.pc:04X}  P={p:02X} [{flags}]",
            f"  Mapper={self.mapper}  Ctrl=0x{self.controller_state:02X}",
            "",
            f"  PPU scan={ppu.scanline} dot={ppu.dot}",
            f"  ctrl={ppu.ctrl:02X} mask={ppu.mask:02X}",
            f"  v={ppu.v:04X} t={ppu.t:04X} fx={ppu.fine_x}",
            f"  NMI={ppu.nmi_occurred} S0hit={ppu.sprite_zero_hit}",
        ]
        tk.Label(d, text="\n".join(lines), justify="left", anchor="nw",
                 bg="#1e1e2e", fg="#e5e9f0", font=("Courier", 11)).pack(
            fill="both", expand=True, padx=12, pady=12)
        tk.Button(d, text="Refresh", command=lambda: (d.destroy(), self.show_debugger()),
                  bg="#3b3b4f", fg="#fff", font=("Courier", 10)).pack(pady=4)

    def on_close(self):
        self.running = False; self.rom_loaded = False
        if self.emulation_thread and self.emulation_thread.is_alive():
            self.emulation_thread.join(timeout=0.3)
        try: self.window.destroy()
        except tk.TclError: pass

    def run(self):
        self.window.mainloop()


# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if _HAS_GUI:
        NESEmulator().run()
    else:
        print("AC NES Emu 0.1 – core loaded OK (tkinter/PIL not available for GUI)")
        print("Install tkinter and Pillow for the graphical frontend.")
