"""The in-game overlay: a small always-on-top window over Rocket League with your MMR, the session's MMR change,
record and other session lines (viewmodel.hud). It's a plain click-through Qt window: nothing is injected into
the game, so it shows over Borderless or Windowed mode, not exclusive Fullscreen. Settings: `hud` in config.json
(the Overlay page). Ctrl+Shift+O hides and shows it (RegisterHotKey on this window, no keyboard hook).
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from . import game, ui, vibrance

ANCHORS = ("tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br")
WHEN = ("front", "running", "always")
LAYOUTS = ("vertical", "horizontal")
HOTKEY_ID = 0x524C  # "RL"
WM_HOTKEY = 0x0312
PREVIEW_S = 10  # Show on screen keeps it up this long


def rl_window() -> int:
    """Rocket League's window when it is the foreground window, else 0."""
    if sys.platform != "win32":
        return 0
    try:
        hwnd = vibrance.foreground_window()
        return hwnd if hwnd and game._process_name(vibrance.window_pid(hwnd)) == "rocketleague.exe" else 0
    except OSError:
        return 0


def window_rect(hwnd: int) -> QRect | None:
    r = wintypes.RECT()
    if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return QRect(r.left, r.top, r.right - r.left, r.bottom - r.top)


class HudWindow(QWidget):
    """Draws the overlay: an optional title and one line (horizontal: one cell) per item, on a dark panel whose
    opacity is `bg`. Click-through and never activated, so Rocket League keeps the keyboard and mouse."""

    hotkey = Signal()

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("RLUtils overlay")
        self.view: dict = {"title": "", "items": []}
        self.cfg: dict = {}
        self._hotkey_on = False
        self._capture = None

    # --- content and size ------------------------------------------------------

    def _fonts(self) -> tuple[QFont, QFont, QFont, QFont]:
        s = self._scale()
        title = QFont(ui.FONT_FAMILY)
        title.setPixelSize(round(12 * s))
        title.setWeight(QFont.DemiBold)
        label = QFont(ui.FONT_FAMILY)
        label.setPixelSize(round(12 * s))
        value, big = self._figures(17 * s), self._figures(30 * s)
        return title, label, value, big

    @staticmethod
    def _figures(px: float) -> QFont:
        """Bahnschrift SemiBold SemiCondensed (ui.FIGURES), picked by weight and width: one variable font."""
        f = QFont(ui.FIGURES_WIDE)
        f.setPixelSize(round(px))
        f.setWeight(QFont.DemiBold)
        f.setStretch(QFont.SemiCondensed)
        return f

    def _scale(self) -> float:
        return max(0.5, min(3.0, int(self.cfg.get("scale", 100)) / 100))

    def set_content(self, view: dict, cfg: dict) -> None:
        self.view, self.cfg = view, cfg
        self.setWindowOpacity(max(0.1, min(1.0, int(cfg.get("opacity", 95)) / 100)))
        self.resize(self._layout()[0])
        self.update()

    def _layout(self):
        """(size, title, [(item, label rect, value rect)]) in widget pixels."""
        s = self._scale()
        pad, gap, bar = round(12 * s), round(4 * s), round(3 * s)
        ft, fl, fv, fb = self._fonts()
        mt, ml, mv, mb = (QFontMetrics(f) for f in (ft, fl, fv, fb))
        items = self.view.get("items") or []
        title = self.view.get("title") or ""
        x0 = pad + bar
        cells = []
        if self.cfg.get("layout") == "horizontal":
            x, h_title = x0, mt.height() if title else 0
            top = pad + (h_title + gap if title else 0)
            row_h = max([(mb if it.get("big") else mv).height() for it in items] or [mv.height()])
            for it in items:
                fm = mb if it.get("big") else mv
                w = max(fm.horizontalAdvance(it["value"]), ml.horizontalAdvance(it["label"].upper()))
                cells.append((it, QRect(x, top + row_h, w, ml.height()), QRect(x, top, w, row_h)))
                x += w + round(18 * s)
            width = max(x - round(18 * s) + pad, x0 + mt.horizontalAdvance(title) + pad if title else 0, x0 + pad)
            height = top + row_h + ml.height() + pad
        else:
            y = pad
            width_l = max([ml.horizontalAdvance(it["label"].upper()) for it in items if not it.get("big")] or [0])
            width_v = max([mv.horizontalAdvance(it["value"]) for it in items if not it.get("big")] or [0])
            inner = max(width_l + round(20 * s) + width_v, mt.horizontalAdvance(title) if title else 0,
                        round(120 * s))
            if title:
                y += mt.height() + gap
            for it in items:
                if it.get("big"):
                    h = mb.height()
                    lw = ml.horizontalAdvance(it["label"])
                    inner = max(inner, mb.horizontalAdvance(it["value"]) + round(8 * s) + lw)
                    cells.append((it, QRect(x0 + mb.horizontalAdvance(it["value"]) + round(8 * s), y, lw, h - round(5 * s)),
                                  QRect(x0, y, mb.horizontalAdvance(it["value"]), h)))
                else:
                    h = mv.height()
                    cells.append((it, QRect(x0, y, width_l, h), QRect(x0, y, inner, h)))
                y += h + gap
            width = x0 + inner + pad
            height = y - (gap if items else 0) + pad
        return QSize(max(width, round(60 * s)), max(height, round(30 * s))), title, cells

    # --- drawing ---------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        size, title, cells = self._layout()
        s = self._scale()
        ft, fl, fv, fb = self._fonts()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        radius = 8 * s
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), radius, radius)
        bg = QColor(18, 18, 20)
        bg.setAlpha(round(255 * max(0, min(100, int(self.cfg.get("bg", 80)))) / 100))
        p.fillPath(path, bg)
        p.setClipPath(path)
        p.fillRect(0, 0, round(3 * s), self.height(), QColor(ui.ACCENT))
        p.setClipping(False)
        pad, bar = round(12 * s), round(3 * s)
        shadow = QColor(0, 0, 0, 170)

        def text(rect: QRect, flags, value: str, font: QFont, color: str) -> None:
            p.setFont(font)
            p.setPen(shadow)  # readable over a bright arena when the panel is see-through
            p.drawText(rect.translated(0, max(1, round(s))), flags, value)
            p.setPen(QColor(color))
            p.drawText(rect, flags, value)

        if title:
            text(QRect(pad + bar, pad, self.width(), QFontMetrics(ft).height()), Qt.AlignLeft | Qt.AlignVCenter,
                 title, ft, "#d9adff")
        horizontal = self.cfg.get("layout") == "horizontal"
        for it, lrect, vrect in cells:
            color = it.get("color") or ui.TEXT
            if horizontal:
                text(vrect, Qt.AlignLeft | Qt.AlignVCenter, it["value"], fb if it.get("big") else fv, color)
                text(lrect, Qt.AlignLeft | Qt.AlignVCenter, it["label"].upper(), fl, ui.MUTED)
            elif it.get("big"):
                text(vrect, Qt.AlignLeft | Qt.AlignVCenter, it["value"], fb, color)
                text(lrect, Qt.AlignLeft | Qt.AlignBottom, it["label"], fl, ui.MUTED)
            else:
                text(lrect, Qt.AlignLeft | Qt.AlignVCenter, it["label"].upper(), fl, ui.MUTED)
                text(vrect, Qt.AlignRight | Qt.AlignVCenter, it["value"], fv, color)
        p.end()

    # --- Windows: topmost, capture, hotkey -------------------------------------

    def _hwnd(self) -> int:
        return int(self.winId())

    def keep_on_top(self) -> None:
        """Rocket League in Borderless can push itself above other topmost windows: put this back above it."""
        if sys.platform == "win32":
            # HWND_TOPMOST; SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
            ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(self._hwnd()), ctypes.c_void_p(-1), 0, 0, 0, 0,
                                              0x0002 | 0x0001 | 0x0010 | 0x0200)

    def set_hide_from_capture(self, on: bool) -> None:
        """WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+): screen recorders and screenshots leave it out."""
        if sys.platform != "win32" or self._capture == on:
            return
        self._capture = on
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(self._hwnd()), 0x11 if on else 0)
        except (AttributeError, OSError):
            pass

    def set_hotkey(self, on: bool) -> bool:
        """Register Ctrl+Shift+O for this window (WM_HOTKEY comes to nativeEvent). False if another program has it."""
        if sys.platform != "win32" or on == self._hotkey_on:
            return True
        user32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(self._hwnd())
        if on:
            ok = bool(user32.RegisterHotKey(hwnd, HOTKEY_ID, 0x0002 | 0x0004 | 0x4000, ord("O")))  # CTRL SHIFT NOREPEAT
            self._hotkey_on = ok
            return ok
        user32.UnregisterHotKey(hwnd, HOTKEY_ID)
        self._hotkey_on = False
        return True

    def nativeEvent(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.hotkey.emit()
                return True, 0
        return False, 0


class HudController(QObject):
    """Shows the overlay when it should be up (enabled, and Rocket League in front / running / always, unless
    hidden with the hotkey), places it on the screen, and feeds it the lines from `content()`. Twice a second."""

    changed = Signal()  # visibility or status changed: the Overlay page shows it

    def __init__(self, parent, cfg, content, rl_running):
        super().__init__(parent)
        self.cfg = cfg                  # () -> the hud settings
        self.content = content          # () -> viewmodel.hud(...)
        self.rl_running = rl_running    # () -> whether Rocket League runs (the app checks every 2 s)
        self.win = HudWindow()
        self.win.winId()  # create the native window now: the hotkey is registered on it
        self.win.hotkey.connect(self.toggle_hidden)
        self.hidden = False             # hidden with the hotkey (until it's pressed again)
        self.preview_until = 0.0
        self.hotkey_ok = True
        self.status = ("off", "Off")
        self._rl_rect: QRect | None = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    def start(self) -> None:
        self.timer.start(500)
        self.tick()

    def stop(self) -> None:
        self.timer.stop()
        self.win.set_hotkey(False)
        self.win.hide()

    def toggle_hidden(self) -> None:
        self.hidden = not self.hidden
        self.preview_until = 0.0
        self.tick()

    def preview(self) -> None:
        """Show on screen: up for PREVIEW_S seconds wherever you are, to check where it sits."""
        self.hidden = False
        self.preview_until = time.monotonic() + PREVIEW_S
        self.tick()
        QTimer.singleShot(PREVIEW_S * 1000 + 100, self.tick)

    def _screen(self, cfg: dict):
        want = cfg.get("screen", "auto")
        screens = QGuiApplication.screens()
        if want != "auto":
            match = next((s for s in screens if s.name() == want), None)
            if match:
                return match
        if self._rl_rect is not None:
            found = QGuiApplication.screenAt(self._rl_rect.center())
            if found:
                return found
        return QGuiApplication.primaryScreen()

    def place(self, cfg: dict) -> None:
        screen = self._screen(cfg)
        if screen is None:
            return
        geo = screen.geometry()
        size = self.win.size()
        anchor = cfg.get("anchor", "tr") if cfg.get("anchor") in ANCHORS else "tr"
        dx, dy = int(cfg.get("dx", 24)), int(cfg.get("dy", 140))
        x = {"l": geo.left() + dx, "c": geo.center().x() - size.width() // 2 + dx,
             "r": geo.right() + 1 - size.width() - dx}[anchor[1]]
        y = {"t": geo.top() + dy, "m": geo.center().y() - size.height() // 2 + dy,
             "b": geo.bottom() + 1 - size.height() - dy}[anchor[0]]
        x = max(geo.left(), min(x, geo.right() + 1 - size.width()))
        y = max(geo.top(), min(y, geo.bottom() + 1 - size.height()))
        if self.win.pos().x() != x or self.win.pos().y() != y:
            self.win.move(x, y)

    def _set_status(self, state: str, text: str) -> None:
        if (state, text) != self.status:
            self.status = (state, text)
            self.changed.emit()

    def tick(self) -> None:
        cfg = self.cfg()
        self.hotkey_ok = self.win.set_hotkey(bool(cfg.get("enabled")) and bool(cfg.get("hotkey", True)))
        previewing = time.monotonic() < self.preview_until
        if not cfg.get("enabled") and not previewing:
            self.win.hide()
            self._set_status("off", "Off")
            return
        hwnd = rl_window()
        self._rl_rect = window_rect(hwnd) if hwnd else self._rl_rect
        when = cfg.get("when", "front")
        if previewing:
            show, text = True, "Showing on screen for a few seconds"
        elif self.hidden:
            show, text = False, "Hidden with Ctrl+Shift+O (press it again to show it)"
        elif when == "always":
            show, text = True, "Showing (always on top)"
        elif when == "running":
            show = self.rl_running()
            text = "Showing while Rocket League runs" if show else "Waiting for Rocket League to start"
        else:
            show = bool(hwnd)
            text = "Showing over Rocket League" if show else "Hidden until Rocket League is in front"
        if not show:
            self.win.hide()
            self._set_status("idle", text)
            return
        self.win.set_content(self.content(), cfg)
        self.place(cfg)
        if not self.win.isVisible():
            self.win.show()
        self.win.set_hide_from_capture(bool(cfg.get("hide_capture")))
        self.win.keep_on_top()
        self._set_status("on", text)
