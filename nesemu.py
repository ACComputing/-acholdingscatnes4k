#!/usr/bin/env python3
"""
AC NES Emu 0.1 – tkinter frontend for Cython NES core.
"""

import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import numpy as np
import threading
import time
import sys
import os
import importlib
import traceback

# Import the compiled Cython module
try:
    nes_core = importlib.import_module("nes_core")
    NES_CORE_AVAILABLE = True
except ImportError:
    NES_CORE_AVAILABLE = False

    class _FallbackNES:
        """
        Minimal pure-Python fallback that keeps the GUI bootable when Cython core
        is not available. It renders a moving color pattern placeholder.
        """
        def __init__(self):
            self._frame = np.zeros((240, 256, 3), dtype=np.uint8)
            self._ticks = 0
            self.controller = [0, 0]
            self.mapper = 0

        def load_rom(self, prg, chr_data):
            self._ticks = 0

        def step_frame(self):
            self._ticks += 1
            t = self._ticks
            y = np.arange(240, dtype=np.uint16)[:, None]
            x = np.arange(256, dtype=np.uint16)[None, :]
            self._frame[..., 0] = (x + t) & 0xFF
            self._frame[..., 1] = (y + (t * 2)) & 0xFF
            self._frame[..., 2] = ((x // 2) ^ (y // 2) ^ t) & 0xFF

        def get_framebuffer(self):
            return self._frame

        def controller_write(self, port, value):
            if 0 <= port <= 1:
                self.controller[port] = value & 0xFF

        def controller_read(self, port):
            if 0 <= port <= 1:
                return self.controller[port]
            return 0

    class _FallbackCoreModule:
        NES = _FallbackNES

    nes_core = _FallbackCoreModule()


class NESEmulator:
    def __init__(self):
        self.window = tk.Tk()
        self.window.title("AC NES Emu 0.1")
        self.window.geometry("600x400")
        self.window.resizable(False, False)
        self.window.configure(bg="#1e1e2e")

        # NES core
        self.nes = nes_core.NES()

        # Display scaling (NES native 256x240 → fit window)
        self.display_scale = 2  # 512x480, fits in 600x400? Actually 512x480 is taller.
        # Let's use a canvas of size 512x480 and allow scrollbars.
        self.canvas = tk.Canvas(self.window, width=512, height=480, bg="black")
        self.canvas.pack(pady=10)
        self.photo = None
        self.canvas_image_id = None

        # Controller mapping (P1) inspired by common emulators
        self.key_map = {
            "z": 0x01,      # A
            "x": 0x02,      # B
            "a": 0x04,      # Select
            "s": 0x08,      # Start
            "Up": 0x10,
            "Down": 0x20,
            "Left": 0x40,
            "Right": 0x80,
        }
        self.controller_state = 0
        self.mapper = 0
        self.black_frame_count = 0

        # Menu
        menubar = tk.Menu(self.window)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Load ROM...", command=self.load_rom)
        file_menu.add_separator()
        file_menu.add_command(label="Pause/Resume", command=self.toggle_pause)
        file_menu.add_command(label="Show Debugger", command=self.show_debugger)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        self.window.config(menu=menubar)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_var.set("No ROM loaded")
        status_bar = tk.Label(self.window, textvariable=self.status_var, bd=1, relief=tk.SUNKEN,
                              anchor=tk.W, bg="#2a2a2e", fg="#ffffff")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # Emulation control
        self.running = True
        self.paused = False
        self.emulation_thread = None
        self.rom_loaded = False

        self.window.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_controller_keys()
        if not NES_CORE_AVAILABLE:
            self.status_var.set("Fallback core active (nes_core not found). GUI booted.")

    def load_rom(self):
        path = filedialog.askopenfilename(
            title="Select NES ROM (iNES format)",
            filetypes=[("NES ROMs", "*.nes"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            # iNES header parser (mapper 0/NROM only in this frontend path)
            if len(data) < 16 or data[0:4] != b'NES\x1a':
                raise ValueError("Not an iNES ROM")

            flags6 = data[6]
            flags7 = data[7]
            prg_size = data[4] * 16384
            chr_size = data[5] * 8192
            mapper = (flags6 >> 4) | (flags7 & 0xF0)
            has_trainer = (flags6 & 0x04) != 0
            mirroring = "vertical" if (flags6 & 0x01) else "horizontal"
            battery = (flags6 & 0x02) != 0

            # NES 2.0 detection (unsupported in this build path)
            if (flags7 & 0x0C) == 0x08:
                raise ValueError("NES 2.0 ROM not supported yet in AC NES Emu 0.1")

            self.mapper = mapper
            offset = 16 + (512 if has_trainer else 0)
            end_prg = offset + prg_size
            end_chr = end_prg + chr_size
            if end_prg > len(data) or end_chr > len(data):
                raise ValueError("ROM file is truncated or header sizes are invalid")

            prg = data[offset:end_prg]
            chr_data = data[end_prg:end_chr]

            self.nes.load_rom(prg, chr_data)
            if hasattr(self.nes, "set_mapper"):
                self.nes.set_mapper(mapper)
            if hasattr(self.nes, "set_mirroring"):
                self.nes.set_mirroring(1 if mirroring == "vertical" else 0)
            if mapper != 0:
                messagebox.showwarning(
                    "Experimental Mapper",
                    f"Mapper {mapper} ROM loaded in best-effort mode.\n"
                    "This build has best compatibility with mapper 0 (NROM).\n"
                    "Commercial mapper-heavy ROMs may boot with glitches or fail to run correctly.",
                )
            self.rom_loaded = True
            self.black_frame_count = 0
            mode = "native" if mapper == 0 else "best-effort"
            self.status_var.set(
                f"Loaded: {os.path.basename(path)} | mapper {mapper} | {mirroring} mirroring"
                + (" | battery" if battery else "")
                + f" | {mode} mode"
            )
            # Start emulation thread if not already running
            if not self.emulation_thread or not self.emulation_thread.is_alive():
                self.running = True
                self.emulation_thread = threading.Thread(target=self.emulation_loop, daemon=True)
                self.emulation_thread.start()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load ROM:\n{e}")

    def _framebuffer_to_rgb_image(self, fb):
        """
        Convert core framebuffer output to a PIL RGB image.
        Supports:
        - uint8[240,256,3] or flat uint8 RGB
        - uint8[240,256,4] RGBA/ARGB-like (drop alpha byte)
        - uint32 packed pixels (0xRRGGBB or 0xAARRGGBB style)
        """
        arr = np.asarray(fb)

        # Direct HxWx3 RGB
        if arr.ndim == 3 and arr.shape[2] == 3:
            rgb = arr.astype(np.uint8, copy=False)
            return Image.fromarray(rgb, "RGB")

        # HxWx4 -> drop alpha channel
        if arr.ndim == 3 and arr.shape[2] == 4:
            rgb = arr[:, :, :3].astype(np.uint8, copy=False)
            return Image.fromarray(rgb, "RGB")

        # Flat/2D uint8 buffer (try RGB first, then RGBA-like)
        if arr.dtype == np.uint8:
            flat = arr.reshape(-1)
            if flat.size == 256 * 240 * 3:
                rgb = flat.reshape(240, 256, 3)
                return Image.fromarray(rgb, "RGB")
            if flat.size == 256 * 240 * 4:
                rgb = flat.reshape(240, 256, 4)[:, :, :3]
                return Image.fromarray(rgb, "RGB")

        # Packed uint32 fallback
        # Assume little-endian 0xAARRGGBB or 0x00RRGGBB and extract RGB bytes.
        packed = arr.astype(np.uint32, copy=False).reshape(-1)
        if packed.size >= 256 * 240:
            p = packed[: 256 * 240]
            r = (p >> 16) & 0xFF
            g = (p >> 8) & 0xFF
            b = p & 0xFF
            rgb = np.stack([r, g, b], axis=1).astype(np.uint8).reshape(240, 256, 3)
            return Image.fromarray(rgb, "RGB")

        raise ValueError(f"Unsupported framebuffer shape/dtype: {arr.shape} / {arr.dtype}")

    def _diagnostic_image(self):
        """Visible fallback frame when core output stays black."""
        y = np.arange(240, dtype=np.uint16)[:, None]
        x = np.arange(256, dtype=np.uint16)[None, :]
        r = ((x // 8) * 11 + (y // 8) * 3) & 0xFF
        g = ((x ^ y) // 2) & 0xFF
        b = ((x * 3 + y * 2) // 5) & 0xFF
        rgb = np.stack([r, g, b], axis=2).astype(np.uint8)
        return Image.fromarray(rgb, "RGB")

    def emulation_loop(self):
        while self.running and self.rom_loaded:
            if not self.paused:
                try:
                    self.nes.step_frame()  # emulates one frame
                    # Get framebuffer and update UI
                    fb = self.nes.get_framebuffer()
                    # Convert framebuffer robustly (RGB / RGBA / packed uint32)
                    img = self._framebuffer_to_rgb_image(fb)

                    mean_luma = float(np.asarray(img, dtype=np.uint8).mean())
                    if mean_luma < 1.5:
                        self.black_frame_count += 1
                    else:
                        self.black_frame_count = 0

                    if self.black_frame_count >= 20:
                        img = self._diagnostic_image()
                        self.status_var.set(
                            f"ROM running but output is blank (mapper {self.mapper}); showing diagnostic frame"
                        )

                    img = img.resize((512, 480), Image.NEAREST)
                    self.photo = ImageTk.PhotoImage(img)
                    try:
                        # UI updates must be guarded during shutdown to avoid TclError.
                        if not self.window.winfo_exists():
                            break
                        if self.canvas_image_id is None:
                            self.canvas_image_id = self.canvas.create_image(0, 0, image=self.photo, anchor=tk.NW)
                        else:
                            self.canvas.itemconfig(self.canvas_image_id, image=self.photo)
                        self.canvas.update_idletasks()
                    except tk.TclError:
                        break
                except Exception as e:
                    self.status_var.set(f"Frame error: {e}")
                    time.sleep(0.016)
            else:
                time.sleep(0.016)  # ~60 fps sleep

    def bind_controller_keys(self):
        for key, value in self.key_map.items():
            self.window.bind(f"<KeyPress-{key}>", lambda e, v=value: self.key_down(v))
            self.window.bind(f"<KeyRelease-{key}>", lambda e, v=value: self.key_up(v))
        self.window.bind("<p>", self.toggle_pause)

    def key_down(self, button):
        self.controller_state |= button
        if hasattr(self.nes, "set_controller_state"):
            self.nes.set_controller_state(0, self.controller_state)
        if hasattr(self.nes, "controller_strobe_write"):
            self.nes.controller_strobe_write(1)
            self.nes.controller_strobe_write(0)
        if hasattr(self.nes, "controller_write"):
            self.nes.controller_write(0, self.controller_state)

    def key_up(self, button):
        self.controller_state &= (~button) & 0xFF
        if hasattr(self.nes, "set_controller_state"):
            self.nes.set_controller_state(0, self.controller_state)
        if hasattr(self.nes, "controller_strobe_write"):
            self.nes.controller_strobe_write(1)
            self.nes.controller_strobe_write(0)
        if hasattr(self.nes, "controller_write"):
            self.nes.controller_write(0, self.controller_state)

    def toggle_pause(self, event=None):
        self.paused = not self.paused
        self.status_var.set("Paused" if self.paused else "Running")

    def show_debugger(self):
        """Lightweight debugger UI placeholder for roadmap phase."""
        dbg = tk.Toplevel(self.window)
        dbg.title("NES Debugger")
        dbg.geometry("360x220")
        dbg.configure(bg="#1e1e2e")
        lines = [
            f"Core: {'Cython' if NES_CORE_AVAILABLE else 'Fallback Python'}",
            f"ROM loaded: {self.rom_loaded}",
            f"Paused: {self.paused}",
            f"Mapper: {self.mapper}",
            f"Controller P1: 0x{self.controller_state:02X}",
        ]
        if hasattr(self.nes, "cpu"):
            cpu = self.nes.cpu
            for reg in ("a", "x", "y", "pc", "sp", "p"):
                if hasattr(cpu, reg):
                    lines.append(f"{reg.upper()}: {getattr(cpu, reg)}")
        tk.Label(dbg, text="\n".join(lines), justify="left", anchor="w",
                 bg="#1e1e2e", fg="#e5e9f0", font=("Courier", 11)).pack(fill="both", expand=True, padx=12, pady=12)

    def on_close(self):
        self.running = False
        self.rom_loaded = False
        # Wait briefly for emulation thread to exit before destroying Tk.
        if self.emulation_thread and self.emulation_thread.is_alive():
            self.emulation_thread.join(timeout=0.25)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.window.mainloop()


if __name__ == "__main__":
    app = NESEmulator()
    app.run()
