"""Look and shared widgets: theme, charts and cards, and dialogs.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .data import playlist_name


# ====================================================================================================
# Theme: the Qt side of the design (dialogs, menus, the tracker.gg check window). The window itself is drawn
# by the web UI in rlutils/web with the same colours: Windows-style greys, purple for you and for actions,
# green and rose only for results.
# ====================================================================================================


# --- colour tokens ------------------------------------------------------------------
BG = "#202020"          # window background
SIDEBAR = "#1C1C1C"     # navigation rail, title and status bars
SUNKEN = "#1C1C1C"      # inputs, segmented tracks
SURFACE = "#2B2B2B"     # panels
SURFACE_2 = "#373737"   # buttons, menus
SURFACE_3 = "#3D3D3D"   # hover, selection
BORDER = "#424242"      # hairlines
TEXT = "#F3F3F3"
MUTED = "#A8A8A8"
FAINT = "#7D7D7D"
ACCENT = "#B65CF2"      # purple: you, primary actions, focus
ACCENT_HOVER = "#C47AF5"
ACCENT_INK = "#1A0B24"  # text on purple
ACCENT_2 = "#B78CFF"
WIN = "#6CCB5F"
LOSS = "#FF99A4"
WARN = "#FCE100"
MANUAL = "#D9ADFF"

# One colour per metal: Bronze ... Grand Champion, then SSL. Index with tier_color(tier_id).
_METAL_COLORS = ["#C08A5B", "#AEB8C8", "#E6B947", "#3FCBC4", "#5AA2FF", "#B084FF", "#FF5F87", "#F3F5FA"]


def tier_color(tier_id: int | None) -> str:
    if not tier_id:
        return FAINT
    return _METAL_COLORS[min(7, (tier_id - 1) // 3)]


# --- type -------------------------------------------------------------------------
FONT_FAMILY = "Segoe UI"                          # body
DISPLAY = "Bahnschrift SemiBold"                  # headings
FIGURES = "Bahnschrift SemiBold SemiCondensed"    # numbers, labels in tight places
FIGURES_WIDE = "Bahnschrift"                      # chart ticks
ASSETS = (Path(__file__).resolve().parent / "assets").as_posix()  # QSS url() wants forward slashes

QSS = f"""
* {{ font-family: '{FONT_FAMILY}'; font-size: 10pt; color: {TEXT}; outline: none; }}
QMainWindow, QWidget#root, QWidget#content {{ background: {BG}; }}
QLabel {{ background: transparent; }}

/* rail */
QWidget#rail {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-family: '{DISPLAY}'; font-size: 13pt; }}
QPushButton#nav {{
    font-family: '{FIGURES}'; font-size: 11pt; text-align: left; color: {MUTED};
    background: transparent; border: none; border-left: 3px solid transparent;
    border-radius: 0; padding: 8px 14px 8px 17px;
}}
QPushButton#nav:hover {{ color: {TEXT}; background: {BG}; }}
QPushButton#nav:checked {{ color: {TEXT}; background: {BG}; border-left: 3px solid {ACCENT}; }}
QPushButton#nav:focus {{ color: {TEXT}; }}
QLabel#railLabel {{ color: {FAINT}; font-size: 8.5pt; }}

/* identity strip */
QFrame#strip {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QLabel#who {{ font-family: '{DISPLAY}'; font-size: 13pt; }}
QLabel#bigMmr {{ font-family: '{FIGURES}'; font-size: 22pt; color: {ACCENT}; }}
QLabel#unit {{ color: {MUTED}; font-size: 9pt; }}
QLabel#pill {{
    border-radius: 11px; padding: 3px 10px; font-size: 9pt;
    background: {SUNKEN}; border: 1px solid {BORDER}; color: {MUTED};
}}
QFrame#vrule {{ background: {BORDER}; max-width: 1px; min-width: 1px; border: none; }}

/* headings */
QLabel#pageTitle {{ font-family: '{DISPLAY}'; font-size: 19pt; }}
QLabel#pageSub {{ color: {MUTED}; font-size: 9.5pt; }}
QLabel#sectionTitle {{ font-family: '{DISPLAY}'; font-size: 11.5pt; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#faint {{ color: {FAINT}; font-size: 8.5pt; }}

/* panels and readouts */
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#readout {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#cell {{ background: transparent; border: none; }}
QLabel#cardLabel {{ color: {MUTED}; font-size: 9pt; }}
QLabel#cardValue {{ font-family: '{FIGURES}'; font-size: 22pt; }}
QLabel#cardSub {{ color: {MUTED}; font-size: 8.5pt; }}

/* buttons */
QPushButton {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 5px;
    padding: 6px 13px; font-weight: 500;
}}
QPushButton:hover {{ background: {SURFACE_3}; }}
QPushButton:pressed {{ background: {SUNKEN}; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton:disabled {{ color: {FAINT}; background: {SURFACE}; }}
QPushButton#primary {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: {ACCENT_INK}; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: #9F45DB; }}
QPushButton#primary:focus {{ border: 1px solid {TEXT}; }}
QPushButton#danger {{ background: transparent; border: 1px solid #6A2F42; color: {LOSS}; }}
QPushButton#danger:hover {{ background: #2A1622; }}

/* segmented control: a sunken track; the chosen option is raised with a boost underline */
QFrame#segBox {{ background: {SUNKEN}; border: 1px solid {BORDER}; border-radius: 6px; }}
QPushButton#seg {{
    font-family: '{FIGURES}'; font-size: 10pt; color: {MUTED};
    background: transparent; border: none; border-radius: 4px; padding: 4px 11px;
    border-bottom: 2px solid transparent;
}}
QPushButton#seg:hover {{ color: {TEXT}; }}
QPushButton#seg:checked {{ color: {TEXT}; background: {SURFACE_2}; border-bottom: 2px solid {ACCENT}; }}
QPushButton#seg:focus {{ color: {TEXT}; }}

/* inputs */
QComboBox, QLineEdit, QSpinBox {{
    background: {SUNKEN}; border: 1px solid {BORDER}; border-radius: 5px;
    padding: 5px 9px; min-height: 18px; selection-background-color: {ACCENT}; selection-color: {ACCENT_INK};
}}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: {SURFACE_3}; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled {{ color: {FAINT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({ASSETS}/chevron.svg); width: 12px; height: 12px; margin-right: 9px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; padding: 4px;
    selection-background-color: {SURFACE_3}; selection-color: {TEXT};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QPlainTextEdit {{ background: {SUNKEN}; border: 1px solid {BORDER}; border-radius: 5px; padding: 6px; }}
QPlainTextEdit:focus {{ border-color: {ACCENT}; }}

QCheckBox {{ spacing: 9px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {FAINT}; background: {SUNKEN}; }}
QCheckBox::indicator:hover {{ border-color: {MUTED}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({ASSETS}/check.svg); }}
QCheckBox:focus {{ color: {ACCENT_HOVER}; }}

QSlider::groove:horizontal {{ height: 4px; background: {SUNKEN}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; border: 2px solid {BG};
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}

/* tables */
QTableWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;
    gridline-color: transparent; alternate-background-color: #2F2F2F;
    selection-background-color: {SURFACE_3}; selection-color: {TEXT};
}}
QTableWidget#flat {{ border: none; border-radius: 0; }}
QTableWidget::item {{ padding: 0 10px; border: none; }}
QHeaderView::section {{
    background: {SURFACE}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 7px 10px; font-family: '{FIGURES}'; font-size: 9.5pt;
}}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SURFACE_3}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {FAINT}; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {SURFACE_3}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; }}
QScrollArea {{ background: transparent; border: none; }}

QLabel#banner {{
    background: #2A2114; color: {WARN}; border: 1px solid #5A4421; border-radius: 8px; padding: 9px 14px;
}}
QStatusBar {{ background: {SIDEBAR}; color: {MUTED}; border-top: 1px solid {BORDER}; font-size: 9pt; }}
QStatusBar::item {{ border: none; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
QMessageBox, QDialog {{ background: {SURFACE}; }}
QMenu {{ background: {SURFACE_2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {SURFACE_3}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
"""


def figures_font(point_size: float, weight: int = QFont.DemiBold) -> QFont:
    font = QFont(FIGURES, 10)
    font.setPointSizeF(point_size)
    font.setWeight(QFont.Weight(weight))
    return font


def make_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(icon_pixmap(size))
    return icon


def icon_pixmap(size: int) -> QPixmap:
    """The design's mark: a purple diamond on a dark rounded tile."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    inset = s * 0.03
    p.setPen(QPen(QColor("#333333"), max(1.0, s * 0.03)))
    p.setBrush(QColor(SIDEBAR))
    p.drawRoundedRect(QRectF(inset, inset, s - 2 * inset, s - 2 * inset), s * 0.22, s * 0.22)
    half = s * 0.27
    diamond = QPainterPath(QPointF(s / 2, s / 2 - half))
    diamond.lineTo(s / 2 + half, s / 2)
    diamond.lineTo(s / 2, s / 2 + half)
    diamond.lineTo(s / 2 - half, s / 2)
    diamond.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    p.drawPath(diamond)
    p.end()
    return pm


def dark_title_bar(widget) -> None:
    """Ask Windows to draw this window's title bar dark (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    if sys.platform != "win32":
        return
    try:
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(widget.winId())), 20, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


def app_font() -> QFont:
    font = QFont(FONT_FAMILY, 10)
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


# ====================================================================================================
# Widgets: Small shared widgets and formatting helpers for the desktop app.
# ====================================================================================================


SHORT_NAMES = {0: "Casual", 1: "Casual 1v1", 2: "Casual 2v2", 3: "Casual 3v3", 4: "Chaos", 10: "1v1",
               11: "2v2", 13: "3v3", 61: "4v4", 27: "Hoops", 28: "Rumble", 29: "Dropshot", 30: "Snowday",
               34: "Tournament", 63: "Heatseeker"}


def short_name(pid: int) -> str:
    return SHORT_NAMES.get(pid, playlist_name(pid))


def local(dt: datetime, fmt: str = "%b %d, %Y  %H:%M") -> str:
    return dt.astimezone().strftime(fmt)


def signed(value: int | float | None, digits: int = 0) -> str:
    if value is None:
        return "--"
    return f"{value:+.{digits}f}" if digits else f"{int(value):+d}"


def tone(value: float | None) -> str:
    if not value:
        return MUTED
    return WIN if value > 0 else LOSS



class Background(QObject):
    """Run a function on a worker thread and get its result back on the UI thread."""

    finished = Signal(object, object, object)  # callback, result, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self.finished.connect(lambda cb, res, err: cb(res, err))

    def run(self, fn, callback) -> None:
        def work():
            try:
                self.finished.emit(callback, fn(), None)
            except Exception as exc:  # reported to the callback
                self.finished.emit(callback, None, exc)
        threading.Thread(target=work, daemon=True).start()


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 15, 18, 15)
        self.body.setSpacing(7)


class StatCard(QFrame):
    """One readout cell: a small label, a big figure, a line of context. Lives inside a Readout."""

    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("cell")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 13, 18, 13)
        self.body.setSpacing(2)
        self.label = QLabel(label)
        self.label.setObjectName("cardLabel")
        self.value = QLabel("--")
        self.value.setObjectName("cardValue")
        self.value_style = ""
        self.sub = QLabel("")
        self.sub.setObjectName("cardSub")
        self.sub.setWordWrap(True)
        for w in (self.label, self.value, self.sub):
            self.body.addWidget(w)
        self.body.addStretch(1)
        self.setMinimumWidth(118)

    def set(self, value: str, sub: str = "", color: str | None = None) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(self.value_style + (f"color: {color};" if color else ""))
        self.sub.setText(sub)
        self.value.setToolTip(value if len(value) > 14 else "")


class Readout(QFrame):
    """A single panel of readout cells split by hairlines, instead of a row of separate cards."""

    def __init__(self, cells, stretches=None):
        super().__init__()
        self.setObjectName("readout")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        for i, c in enumerate(cells):
            if i:
                row.addWidget(vrule())
            row.addWidget(c, (stretches or [1] * len(cells))[i])


def vrule() -> QFrame:
    rule = QFrame()
    rule.setObjectName("vrule")
    rule.setFixedWidth(1)
    return rule


class RankRail(QWidget):
    """The division gauge: from this division's start to the next one's, with your MMR as the marker.

    Purely presentational; the app passes in numbers it already computed.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.state: dict | None = None

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(600, 92)

    def set_state(self, *, rank: str, color: str, rating: int | None, next_label: str | None = None,
                  to_next: int | None = None, to_drop: int | None = None, prev_label: str | None = None,
                  estimated: bool = False, note: str = "") -> None:
        self.state = dict(rank=rank, color=color, rating=rating, next_label=next_label, to_next=to_next,
                          to_drop=to_drop, prev_label=prev_label, estimated=estimated, note=note)
        self.update()

    def clear(self, note: str) -> None:
        self.state = {"empty": note}
        self.update()

    def paintEvent(self, _event) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QFont, QPainter, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad = 20
        st = self.state or {"empty": ""}
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(SURFACE))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 8, 8)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 8, 8)
        if "empty" in st:
            p.setPen(QColor(MUTED))
            p.setFont(QFont(FONT_FAMILY, 10))
            p.drawText(QRectF(pad, 0, w - 2 * pad, h), Qt.AlignVCenter | Qt.AlignLeft, st["empty"])
            p.end()
            return

        color = QColor(st["color"])
        # Top line: current division (tier colour) on the left, the next division and the gap on the right.
        p.setFont(figures_font(14))
        p.setPen(color)
        p.drawText(QRectF(pad, 10, w / 2, 26), Qt.AlignLeft | Qt.AlignVCenter, st["rank"])
        right = ""
        if st["next_label"] and st["to_next"] is not None:
            right = f"{'about ' if st['estimated'] else ''}{st['to_next']} to {st['next_label']}"
        elif st["next_label"]:
            right = f"Next: {st['next_label']}"
        p.setFont(figures_font(11, QFont.Normal))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(w / 2, 10, w / 2 - pad, 26), Qt.AlignRight | Qt.AlignVCenter, right)

        # The track.
        ty, th = 46, 10
        track = QRectF(pad, ty, w - 2 * pad, th)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(SUNKEN))
        p.drawRoundedRect(track, th / 2, th / 2)
        frac = None
        if st["to_next"] is not None and st["to_drop"] is not None and (st["to_next"] + st["to_drop"]) > 0:
            frac = max(0.0, min(1.0, st["to_drop"] / (st["to_next"] + st["to_drop"])))
        # Quarter ticks, so the eye can read the position without numbers.
        p.setPen(QPen(QColor(BORDER), 1))
        for q in (0.25, 0.5, 0.75):
            x = track.left() + track.width() * q
            p.drawLine(int(x), int(ty + th + 3), int(x), int(ty + th + 6))
        if frac is not None:
            fill = QRectF(track.left(), ty, max(th, track.width() * frac), th)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(fill, th / 2, th / 2)
            mx = track.left() + track.width() * frac
            p.setBrush(QColor(ACCENT))
            p.setPen(QPen(QColor(SURFACE), 3))
            p.drawEllipse(QRectF(mx - 8, ty + th / 2 - 8, 16, 16))
        else:
            p.setPen(QPen(QColor(FAINT), 1, Qt.DashLine))
            p.drawLine(int(track.left() + 6), int(ty + th / 2), int(track.right() - 6), int(ty + th / 2))

        # Bottom line: the division's MMR window, and where the numbers come from.
        p.setFont(figures_font(9.5, QFont.Normal))
        p.setPen(QColor(MUTED))
        rating = st["rating"]
        if frac is not None and rating is not None:
            low, high = rating - st["to_drop"], rating + st["to_next"]
            p.drawText(QRectF(pad, ty + th + 8, 200, 20), Qt.AlignLeft | Qt.AlignVCenter, f"{low}")
            p.drawText(QRectF(w - pad - 200, ty + th + 8, 200, 20), Qt.AlignRight | Qt.AlignVCenter, f"{high}")
        mid = st["note"] or ("Estimated from your readings" if st["estimated"] else "")
        if frac is None and not mid:
            mid = "Not enough readings to place you in this division yet"
        p.setPen(QColor(FAINT))
        p.setFont(QFont(FONT_FAMILY, 8))
        p.drawText(QRectF(pad + 60, ty + th + 8, w - 2 * pad - 120, 20), Qt.AlignHCenter | Qt.AlignVCenter, mid)
        p.end()


class Segmented(QFrame):
    """A row of mutually exclusive buttons. Calls `on_change(data)` when one is clicked."""

    def __init__(self, on_change):
        super().__init__()
        self.setObjectName("segBox")
        self.on_change = on_change
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(3, 3, 3, 3)
        self.row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self._clicked)
        self.items: list = []

    def set_items(self, items: list[tuple[str, object]], selected=None) -> None:
        for btn in self.group.buttons():
            self.group.removeButton(btn)
            btn.deleteLater()
        self.items = [data for _label, data in items]
        for i, (label, _data) in enumerate(items):
            btn = QPushButton(label)
            btn.setObjectName("seg")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            self.group.addButton(btn, i)
            self.row.addWidget(btn)
        if self.items:
            idx = self.items.index(selected) if selected in self.items else 0
            self.group.button(idx).setChecked(True)

    def current(self):
        idx = self.group.checkedId()
        return self.items[idx] if 0 <= idx < len(self.items) else None

    def select(self, data) -> None:
        """Check the button for `data`, or clear the selection when it isn't one of the buttons."""
        self.group.setExclusive(False)
        for i, btn in enumerate(self.group.buttons()):
            btn.setChecked(self.items[i] == data)
        self.group.setExclusive(True)

    def _clicked(self, idx: int) -> None:
        self.on_change(self.items[idx])



def page_header(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    t = QLabel(title)
    t.setObjectName("pageTitle")
    s = QLabel(subtitle)
    s.setObjectName("pageSub")
    s.setWordWrap(True)
    v.addWidget(t)
    v.addWidget(s)
    w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    return w


def section(title: str) -> QLabel:
    label = QLabel(title)
    label.setObjectName("sectionTitle")
    return label


def muted(text: str, wrap: bool = True, name: str = "muted") -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label
