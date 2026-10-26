"""Tray icon with a styled right-click menu.

The native tray menu cannot be themed, so a right-click opens a small dark popup
in the dashboard's colors instead: live status, game ranks and the usual
actions. It runs on its own Tk thread. If Tk is unavailable or the popup fails,
the native menu is shown as before.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

import pystray
from PIL import Image

log = logging.getLogger(__name__)

BG = "#070b11"
PANEL = "#0b121b"
LINE = "#1a2634"
TXT = "#c7d6e5"
BRIGHT = "#eef4fa"
MUTED = "#5f7285"
CYAN = "#00e5ff"
MAG = "#ff2d95"
GOLD = "#ffe200"
GREEN = "#25f4a7"
AMBER = "#ffb300"
RED = "#ff4d5e"
HOVER = "#0e2230"
DOT = {"on": GREEN, "warn": AMBER, "off": RED, "busy": MAG, "": MUTED}

_WM_RBUTTONUP = 0x0205
_ICON_PATH = Path(__file__).resolve().parent / "webui" / "static" / "tpp.png"


def tray_image(state: str) -> Image.Image | None:
    """The TPP skull with a status dot: green ready, magenta uploading, gray idle."""
    try:
        base = tray_image.base  # type: ignore[attr-defined]
    except AttributeError:
        try:
            base = Image.open(_ICON_PATH).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except OSError:
            base = None
        tray_image.base = base  # type: ignore[attr-defined]
    if base is None:
        return None
    from PIL import ImageDraw
    img = base.copy()
    d = ImageDraw.Draw(img)
    color = {"busy": (255, 45, 149, 255), "on": (37, 244, 167, 255)}.get(state, (127, 140, 141, 255))
    d.ellipse((40, 40, 63, 63), fill=(7, 11, 17, 255))
    d.ellipse((44, 44, 59, 59), fill=color)
    return img


class TrayIcon(pystray.Icon):
    """pystray icon that hands right-clicks to a custom popup. If the popup
    cannot open, the native menu is used."""

    def __init__(self, *args: Any, on_right_click: Callable[[], bool] | None = None, **kwargs: Any) -> None:
        self._on_right_click = on_right_click
        super().__init__(*args, **kwargs)

    def _on_notify(self, wparam, lparam):
        if lparam == _WM_RBUTTONUP and self._on_right_click:
            try:
                if self._on_right_click():
                    return
            except Exception:
                log.exception("tray popup failed")
        super()._on_notify(wparam, lparam)


def fmt_duration(s: float) -> str:
    s = max(0, int(s))
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h {m}m" if h else (f"{m}m" if m else f"{s}s")


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect),
                ("rcWork", _Rect), ("dwFlags", wintypes.DWORD)]


def _cursor_and_workarea() -> tuple[int, int, tuple[int, int, int, int]]:
    u = ctypes.windll.user32
    pt = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    mon = u.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MonitorInfo()
    mi.cbSize = ctypes.sizeof(_MonitorInfo)
    u.GetMonitorInfoW(mon, ctypes.byref(mi))
    w = mi.rcWork
    return pt.x, pt.y, (w.left, w.top, w.right, w.bottom)


class TrayPopup:
    """The styled menu. ``model`` returns the live values to show; ``actions``
    maps action names to callables."""

    WIDTH = 330

    def __init__(self, title: str, version: str, model: Callable[[], dict],
                 actions: dict[str, Callable[..., Any]]) -> None:
        self.title, self.version = title, version
        self.model, self.actions = model, actions
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._ok: bool | None = None
        self._thread: threading.Thread | None = None
        self._win = None
        self._opened = 0.0

    # -- called from the tray thread ---------------------------------------
    def show(self) -> bool:
        if sys.platform != "win32" or self._ok is False:
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="wdgw-traymenu", daemon=True)
            self._thread.start()
        self._ready.wait(5)
        if not self._ok:
            return False
        self._q.put("show")
        return True

    # -- Tk thread ---------------------------------------------------------
    def _run(self) -> None:
        try:
            try:   # crisp text on high-DPI screens, for windows made on this thread
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            import tkinter as tk
            from tkinter import font as tkfont
            from PIL import ImageTk
            self.tk, self.ImageTk = tk, ImageTk
            self.root = tk.Tk()
            self.root.withdraw()
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
            fams = set(tkfont.families(self.root))
            mono = next((f for f in ("Cascadia Code", "Cascadia Mono", "Consolas") if f in fams), "Courier New")
            self.f_title = tkfont.Font(family=mono, size=12, weight="bold")
            self.f_sub = tkfont.Font(family=mono, size=8)
            self.f_lbl = tkfont.Font(family=mono, size=9)
            self.f_val = tkfont.Font(family=mono, size=9, weight="bold")
            self.f_item = tkfont.Font(family=mono, size=10)
            self.f_item_b = tkfont.Font(family=mono, size=10, weight="bold")
            self.f_sym = tkfont.Font(family="Segoe UI Symbol", size=11)
            self.f_chip = tkfont.Font(family=mono, size=9, weight="bold")
            px = int(40 * self.scale)
            try:
                src = Image.open(_ICON_PATH).convert("RGBA")
                self.logo = ImageTk.PhotoImage(src.resize((px, px), Image.LANCZOS))
                sm = int(18 * self.scale)
                self.logo_sm = ImageTk.PhotoImage(src.resize((sm, sm), Image.LANCZOS))
            except OSError:
                self.logo = self.logo_sm = None
            self._ok = True
        except Exception:
            log.exception("styled tray menu unavailable; using the native menu")
            self._ok = False
            self._ready.set()
            return
        self._ready.set()
        self.root.after(40, self._poll)
        self.root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                if msg == "show":
                    self._close()
                    self._open()
        except queue.Empty:
            pass
        except Exception:
            log.exception("tray popup error")
        self.root.after(40, self._poll)

    def _px(self, v: float) -> int:
        return int(round(v * self.scale))

    def _close(self) -> None:
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None

    def _open(self) -> None:
        tk = self.tk
        m = self.model()
        win = tk.Toplevel(self.root)
        self._win = win
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.configure(bg=CYAN)
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self._vals: dict[str, tuple] = {}
        self._build(body, m)
        win.bind("<Escape>", lambda e: self._close())

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x, y, (l, t, r, b) = _cursor_and_workarea()
        gx = min(max(l + 4, x - w + self._px(12)), r - w - 4)
        gy = y - h - self._px(8) if y - h - self._px(8) >= t else y + self._px(8)
        gy = min(max(t + 4, gy), b - h - 4)
        win.geometry(f"{w}x{h}+{gx}+{gy}")
        win.deiconify()
        win.update_idletasks()
        try:
            hwnd = int(win.wm_frame(), 16)
        except (ValueError, TypeError):
            hwnd = win.winfo_id()
        self._hwnd = hwnd
        self._had_focus = False
        self._style_frame(hwnd, body)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        win.focus_force()
        self._opened = time.monotonic()
        self._fade(win, 0.0)
        win.after(150, self._watch)
        win.after(1000, self._refresh)

    def _style_frame(self, hwnd: int, body) -> None:
        """Windows 11: rounded corners and a cyan system border instead of ours."""
        try:
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(2)    # DWMWCP_ROUND
            color = ctypes.c_uint(0x00FFE500)  # cyan as COLORREF (0x00BBGGRR)
            a = dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
            c = dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), 4)
            if a == 0 and c == 0:
                body.pack_configure(padx=0, pady=0)
        except Exception:
            pass

    def _fade(self, win, a: float) -> None:
        if self._win is not win:
            return
        a = min(1.0, a + 0.16)
        try:
            win.attributes("-alpha", a)
        except Exception:
            return
        if a < 1.0:
            win.after(15, lambda: self._fade(win, a))

    def _watch(self) -> None:
        """Close on a click outside the popup, or when another window takes focus."""
        win = self._win
        if win is None:
            return
        u = ctypes.windll.user32
        settled = time.monotonic() - self._opened > 0.3
        fg = u.GetForegroundWindow()
        if fg == self._hwnd:
            self._had_focus = True
        elif self._had_focus and settled:
            self._close()
            return
        if settled and any(u.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02, 0x04)):
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            r = _Rect()
            u.GetWindowRect(self._hwnd, ctypes.byref(r))
            if not (r.left <= pt.x < r.right and r.top <= pt.y < r.bottom):
                self._close()
                return
        win.after(60, self._watch)

    # -- content -----------------------------------------------------------
    def _build(self, body, m: dict) -> None:
        tk = self.tk
        head = tk.Frame(body, bg=PANEL)
        head.pack(fill="x")
        inner = tk.Frame(head, bg=PANEL)
        inner.pack(fill="x", padx=self._px(14), pady=(self._px(12), self._px(10)))
        if self.logo:
            tk.Label(inner, image=self.logo, bg=PANEL).pack(side="left", padx=(0, self._px(10)))
        tt = tk.Frame(inner, bg=PANEL)
        tt.pack(side="left", fill="x", expand=True)
        title = tk.Frame(tt, bg=PANEL)
        title.pack(anchor="w")
        words = self.title.upper().split(" ", 1)
        tk.Label(title, text=words[0], font=self.f_title, fg=CYAN, bg=PANEL).pack(side="left")
        if len(words) > 1:
            tk.Label(title, text=" " + words[1], font=self.f_title, fg=MAG, bg=PANEL).pack(side="left")
        self._sub = tk.Label(tt, text="", font=self.f_sub, fg=MUTED, bg=PANEL, anchor="w")
        self._sub.pack(anchor="w")
        tk.Frame(body, bg=CYAN, height=1).pack(fill="x")

        st = tk.Frame(body, bg=BG)
        st.pack(fill="x", padx=self._px(14), pady=(self._px(10), self._px(4)))
        st.columnconfigure(1, weight=1)
        for i, key in enumerate(("adsb", "mesh", "queue", "next")):
            dot = tk.Canvas(st, width=self._px(9), height=self._px(9), bg=BG, highlightthickness=0)
            dot.grid(row=i, column=0, sticky="w", pady=self._px(2))
            oval = dot.create_oval(1, 1, self._px(8), self._px(8), fill=MUTED, outline="")
            lbl = tk.Label(st, text="", font=self.f_lbl, fg=MUTED, bg=BG, anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=(self._px(8), 0))
            val = tk.Label(st, text="", font=self.f_val, fg=BRIGHT, bg=BG, anchor="e")
            val.grid(row=i, column=2, sticky="e")
            self._vals[key] = (dot, oval, lbl, val)

        games = [g for g in m.get("games") or [] if g]
        if games:
            gf = tk.Frame(body, bg=BG)
            gf.pack(fill="x", padx=self._px(14), pady=(self._px(4), self._px(6)))
            for g in games:
                row = tk.Frame(gf, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
                row.pack(fill="x", pady=self._px(2))
                tk.Label(row, text=g["game"], font=self.f_sub, fg=MUTED, bg=PANEL, width=9,
                         anchor="w").pack(side="left", padx=(self._px(8), 0), pady=self._px(4))
                tk.Label(row, text=g["name"], font=self.f_val, fg=g["color"], bg=PANEL,
                         anchor="w").pack(side="left")
                if g.get("rank"):
                    tk.Label(row, text=g["rank"], font=self.f_val, fg=BRIGHT, bg=PANEL).pack(
                        side="right", padx=self._px(8))
        self._update(m)

        tk.Frame(body, bg=LINE, height=1).pack(fill="x", pady=(self._px(4), 0))
        items = tk.Frame(body, bg=BG)
        items.pack(fill="x", pady=self._px(4))
        self._item(items, "▲", "Upload now", "upload", accent=CYAN, bold=True)
        self._item(items, "◉", "Open dashboard", "dashboard")
        self._item(items, "⌖", "Live map", "map")
        self._interval_row(items, m.get("interval", 4))
        self._boot_item = self._item(items, "☑" if m.get("boot") else "☐", "Start with Windows", "boot", close=False)
        self._item(items, "▤", "Open log folder", "folder")
        tk.Frame(body, bg=LINE, height=1).pack(fill="x")
        links = tk.Frame(body, bg=BG)
        links.pack(fill="x", pady=self._px(4))
        self._item(links, "☠", "The Pirates' Plunder", "tpp", accent=GOLD, image=self.logo_sm)
        self._item(links, "↗", "rocketgod.dev", "site", accent=MAG)
        tk.Frame(body, bg=LINE, height=1).pack(fill="x")
        q = tk.Frame(body, bg=BG)
        q.pack(fill="x", pady=self._px(4))
        self._item(q, "⏻", "Quit", "quit", hover_fg=RED)
        body.configure(width=self._px(self.WIDTH))

    def _item(self, parent, sym: str, text: str, action: str, accent: str = TXT,
              bold: bool = False, close: bool = True, hover_fg: str | None = None, image=None):
        tk = self.tk
        row = tk.Frame(parent, bg=BG, cursor="hand2")
        row.pack(fill="x")
        if image is not None:
            s = tk.Label(row, image=image, bg=BG, width=self._px(22), cursor="hand2")
        else:
            s = tk.Label(row, text=sym, font=self.f_sym, fg=accent if accent != TXT else CYAN, bg=BG,
                         width=2, cursor="hand2")
        s.pack(side="left", padx=(self._px(12), self._px(6)), pady=self._px(4))
        t = tk.Label(row, text=text, font=self.f_item_b if bold else self.f_item, fg=accent, bg=BG,
                     anchor="w", cursor="hand2")
        t.pack(side="left", fill="x", expand=True)
        parts = (row, s, t)

        def enter(_e):
            for w in parts:
                w.configure(bg=HOVER)
            if hover_fg:
                t.configure(fg=hover_fg)
                if image is None:
                    s.configure(fg=hover_fg)

        def leave(_e):
            for w in parts:
                w.configure(bg=BG)
            if image is None:
                s.configure(fg=accent if accent != TXT else CYAN)
            t.configure(fg=accent)

        def click(_e):
            if close:
                self._close()
            self._fire(action)
            if not close:
                self._after_toggle(action, s)

        for w in parts:
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
            w.bind("<Button-1>", click)
        return s

    def _interval_row(self, parent, current: int) -> None:
        tk = self.tk
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(self._px(2), self._px(4)))
        tk.Label(row, text="⟳", font=self.f_sym, fg=CYAN, bg=BG, width=2).pack(
            side="left", padx=(self._px(12), self._px(6)))
        tk.Label(row, text="Every", font=self.f_item, fg=TXT, bg=BG).pack(side="left", padx=(0, self._px(8)))
        self._chips = {}
        for h in (1, 4, 12, 24):
            c = tk.Label(row, text=f"{h}h", font=self.f_chip, bg=BG, cursor="hand2",
                         padx=self._px(7), pady=self._px(1), highlightthickness=1)
            c.pack(side="left", padx=self._px(2))
            c.bind("<Button-1>", lambda _e, hh=h: self._set_interval(hh))
            self._chips[h] = c
        self._paint_chips(current)

    def _paint_chips(self, current: int) -> None:
        for h, c in self._chips.items():
            on = h == int(current)
            c.configure(fg=BG if on else MUTED, bg=CYAN if on else BG,
                        highlightbackground=CYAN if on else LINE, highlightcolor=CYAN if on else LINE)

    def _set_interval(self, h: int) -> None:
        self._paint_chips(h)
        self._fire("interval", h)

    def _after_toggle(self, action: str, sym) -> None:
        if action == "boot" and self._win is not None:
            self._win.after(300, lambda: sym.configure(text="☑" if self.model().get("boot") else "☐"))

    def _fire(self, action: str, *args: Any) -> None:
        fn = self.actions.get(action)
        if fn:
            threading.Thread(target=self._safe, args=(fn, args), daemon=True).start()

    @staticmethod
    def _safe(fn, args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("tray action failed")

    def _refresh(self) -> None:
        if self._win is None:
            return
        try:
            self._update(self.model())
        except Exception:
            log.exception("tray popup refresh failed")
        self._win.after(1000, self._refresh)

    def _update(self, m: dict) -> None:
        self._sub.configure(text=f"v{self.version}  ·  {m.get('activity', 'idle')}")
        for key, (dot, oval, lbl, val) in self._vals.items():
            state, label, value = m.get(key) or ("", key, "")
            dot.itemconfigure(oval, fill=DOT.get(state, MUTED))
            lbl.configure(text=label)
            val.configure(text=value)
