"""只显示最近十行日志；置顶、鼠标穿透，不激活游戏以外的窗口。"""
from collections import deque
import ctypes
from ctypes import wintypes
import logging
import time
import tkinter as tk


class LogBuffer(logging.Handler):
    def __init__(self, max_lines=10):
        super().__init__()
        self.lines = deque(maxlen=max_lines)
        self.revision = 0

    def emit(self, record):
        self.lines.extend(self.format(record).splitlines())
        self.revision += 1


def overlay_layout(bounds):
    left, top, width, height = bounds
    scale = max(0.7, min(3.0, height / 1080))
    font_pixels = max(12, round(17 * scale))
    panel_width = round(min(width * 0.42, 700 * scale))
    panel_height = round(font_pixels * 1.55 * 11 + 16 * scale)
    return left + round(12 * scale), top + round(68 * height / 720), panel_width, panel_height, font_pixels


class LogOverlay:
    def __init__(self, buffer):
        self.buffer = buffer
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.78)
        self.root.configure(background='#101820')
        self.label = tk.Label(self.root, text='', background='#101820', foreground='#c8f5d5',
                              anchor='nw', justify='left', padx=10, pady=8, takefocus=False)
        self.label.pack(fill='both', expand=True)
        self.root.update_idletasks()
        self.user32 = ctypes.windll.user32
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.handle = self.user32.GetAncestor(self.root.winfo_id(), 2)
        get_style = getattr(self.user32, 'GetWindowLongPtrW', self.user32.GetWindowLongW)
        set_style = getattr(self.user32, 'SetWindowLongPtrW', self.user32.SetWindowLongW)
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE。
        styles = get_style(self.handle, -20) | 0x80000 | 0x20 | 0x80 | 0x8000000
        set_style(self.handle, -20, styles)
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.previous_bounds = None
        self.previous_revision = -1
        self.last_raise_at = 0.0
        self.closed = False

    def update(self, bounds):
        changed = bounds != self.previous_bounds
        if changed:
            left, top, width, height, font_pixels = overlay_layout(bounds)
            self.label.configure(font=('Microsoft YaHei UI', -font_pixels))
            self.position = (left, top, width, height)
            self.previous_bounds = bounds
        if self.buffer.revision != self.previous_revision:
            self.label.configure(text='钓鱼日志 · 最近 10 行\n' + '\n'.join(self.buffer.lines))
            self.previous_revision = self.buffer.revision
        now = time.monotonic()
        if changed or now - self.last_raise_at >= 0.5:
            if self.root.state() == 'withdrawn':
                self.root.deiconify()
            # HWND_TOPMOST；SWP_NOACTIVATE | SWP_SHOWWINDOW，不抢游戏焦点。
            if not self.user32.SetWindowPos(self.handle, -1, *self.position, 0x10 | 0x40):
                raise RuntimeError('日志浮层定位失败。')
            self.last_raise_at = now
        self.root.update_idletasks()
        self.root.update()

    def close(self):
        if not self.closed:
            self.root.destroy()
            self.closed = True
