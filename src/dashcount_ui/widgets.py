"""The dashboard's custom-painted widgets."""

from __future__ import annotations

import datetime as dt
import html
import math
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QFrame, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget

from . import pixel as px
from .pixel import col, font_px, font_vt

ET = ZoneInfo("America/New_York")


def et_hm(ts: float | None) -> str:
    if not ts:
        return "--:--"
    return dt.datetime.fromtimestamp(ts, tz=ET).strftime("%H:%M")


def money(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "--"
    return f"{'+' if sign and v >= 0 else ''}{'-' if v < 0 else ''}${abs(v):,.2f}"


# ------------------------------------------------------------------ panels
class PixelPanel(QFrame):
    """Deepslate GUI panel with a pixel-font title; children go in .body."""

    def __init__(self, title: str, icon: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.icon = px.icon_pixmap(icon, 2) if icon else None
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(10, 30, 10, 10)
        self.body.setSpacing(6)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        px.panel(p, self.rect())
        x = 12
        if self.icon:
            p.drawPixmap(10, 9, self.icon)
            x = 10 + self.icon.width() + 6
        px.title(p, x, 22, self.title, 10, "parch", "black")
        p.end()


class ParchmentPanel(QFrame):
    def __init__(self, title: str, parent=None, seed: int = 3) -> None:
        super().__init__(parent)
        self.title = title
        self.seed = seed
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(12, 30, 12, 10)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        px.parchment(p, self.rect(), self.seed)
        px.title(p, 14, 23, self.title, 10, "ink", "parch2")
        p.end()


# ------------------------------------------------------------------ header
class Header(QWidget):
    chip_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(96)
        self.chips: list[tuple[str, str, str, str]] = []      # (key, text, bg, fg)
        self.subtitle = "the treasure hunter's trading table"
        self._hit: list[tuple[QRect, str]] = []

    def set_chips(self, chips: list[tuple[str, str, str, str]]) -> None:
        self.chips = chips
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        r = self.rect()
        px.grass_strip(p, QRect(0, 0, r.width(), 18))
        p.drawPixmap(0, 18, px.texture("stone", r.width(), r.height() - 18, 8))
        p.fillRect(QRect(0, r.height() - 3, r.width(), 3), col("black"))
        p.drawPixmap(16, 34, px.icon_pixmap("pickaxe", 3))
        px.title(p, 76, 58, "DASHCOUNT", 22, "gold", "ink")
        p.setFont(font_vt(20))
        p.setPen(col("muted"))
        p.drawText(78, 82, self.subtitle)
        # chips, right-aligned, wrapping to two rows if needed
        self._hit = []
        p.setFont(font_px(9))
        fm = p.fontMetrics()
        x = r.width() - 12
        y = 30
        for key, text, bg, fg in reversed(self.chips):
            w = fm.horizontalAdvance(text) + 18
            if x - w < 420:
                x = r.width() - 12
                y += 32
            cr = QRect(x - w, y, w, 26)
            px.bevel(p, cr, bg, "slot_hi" if bg == "slot" else bg, "black", "black", 2)
            p.setPen(col(fg))
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, text)
            self._hit.append((cr, key))
            x -= w + 6
        p.end()

    def mousePressEvent(self, e) -> None:
        for rect, key in self._hit:
            if rect.contains(e.position().toPoint()):
                self.chip_clicked.emit(key)


# --------------------------------------------------------------- the level
class LevelView(QWidget):
    """Watchlist rows with a centred count meter: sellers <- | -> buyers."""

    ROW = 28

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []
        self.market: dict = {}
        self.setMinimumHeight(self.ROW * 8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(self, symbols: list[dict], market: dict) -> None:
        stocks = [s for s in symbols if not s.get("market")]
        stocks.sort(key=lambda s: (not s.get("busy"), -abs(s.get("tc_long") or 0)))
        self.rows = stocks
        self.market = market or {}
        self.setMinimumHeight(self.ROW * (len(stocks) + 2) + 8)
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        w = self.width()
        p.setFont(font_px(8))
        p.setPen(col("muted"))
        p.drawText(4, 12, "STOCK")
        p.drawText(int(w * 0.19), 12, "PRICE")
        p.drawText(int(w * 0.36), 12, "DAY")
        mx, mw = self._meter_box(w)
        p.drawText(mx, 12, "SELL")
        p.drawText(mx + mw - 34, 12, "BUY")
        y = 20
        if not self.rows:
            p.setFont(font_vt(22))
            p.setPen(col("muted"))
            p.drawText(QRect(0, 40, w, 60), Qt.AlignmentFlag.AlignCenter, "No level loaded yet.\nStart the day to build the map.")
        for s in self.rows:
            self._row(p, y, w, s)
            y += self.ROW
        mz = self.market.get("z")
        if mz is not None:
            y += 6
            p.fillRect(QRect(0, y - 4, w, 2), col("deepslate_hi"))
            p.setFont(font_vt(20))
            p.setPen(col("parch"))
            p.drawText(4, y + 18, "MARKET FLOW")
            mx, mw = self._meter_box(w)
            self._meter(p, mx, y + 4, mw, mz)
        p.end()

    def _row(self, p: QPainter, y: int, w: int, s: dict) -> None:
        if s.get("busy"):
            p.fillRect(QRect(0, y, w, self.ROW - 2), col("deepslate_hi"))
        p.setFont(font_vt(22))
        p.setPen(col("gold") if s.get("busy") else col("text"))
        p.drawText(4, y + 20, s["symbol"])
        price = s.get("price")
        chg = s.get("change_pct")
        p.setPen(col("text"))
        p.drawText(int(w * 0.19), y + 20, f"{price:,.2f}" if price else "--")
        if chg is not None:
            p.setPen(col("good") if chg >= 0 else col("bad"))
            p.drawText(int(w * 0.36), y + 20, f"{chg:+.1f}%")
        mx, mw = self._meter_box(w)
        self._meter(p, mx, y + 6, mw, s.get("tc_long") or 0.0)
        ix = w - 20
        if s.get("busy"):
            p.drawPixmap(ix, y + 5, px.icon_pixmap("sword", 2))
        elif (s.get("spread_ratio") or 0) > 2 or (s.get("last_trade_age") or 0) > 180:
            p.drawPixmap(ix, y + 5, px.icon_pixmap("spike", 2))

    @staticmethod
    def _meter_box(w: int) -> tuple[int, int]:
        """x and width of the meter area (leaves room for the value and an icon)."""
        x = int(w * 0.50)
        return x, max(120, w - x - 70)

    @staticmethod
    def _meter(p: QPainter, x: int, y: int, width: int, v: float) -> None:
        """5 blocks each side; each block = 0.6 sigma; gold when |v| >= 3 (go big)."""
        n = 5
        bw = max(5, (width - 6) // (2 * n) - 2)
        cx = x + n * (bw + 2)
        p.fillRect(QRect(cx, y - 2, 2, 18), col("parch"))
        filled = min(n, int(abs(v) / 0.6 + 0.5))
        for i in range(n):
            for side in (-1, 1):
                bx = cx + 4 + i * (bw + 2) if side > 0 else cx - (i + 1) * (bw + 2)
                on = (side > 0 and v > 0 and i < filled) or (side < 0 and v < 0 and i < filled)
                if on:
                    c = "gold" if abs(v) >= 3 else ("emerald" if side > 0 else "lava")
                else:
                    c = "deepslate_lo"
                p.fillRect(QRect(bx, y, bw, 14), col("black"))
                p.fillRect(QRect(bx + 1, y + 1, bw - 2, 12), col(c))
        p.setFont(font_vt(18))
        p.setPen(col("gold") if abs(v) >= 3 else col("text"))
        p.drawText(x + 2 * n * (bw + 2) + 12, y + 13, f"{v:+.1f}")


# --------------------------------------------------------------- inventory
class InventoryView(QWidget):
    SLOT_H = 74

    def __init__(self, slots: int = 3, parent=None) -> None:
        super().__init__(parent)
        self.slots = slots
        self.trades: list[dict] = []
        self.setMinimumHeight(slots * (self.SLOT_H + 6))

    def set_trades(self, trades: list[dict]) -> None:
        self.trades = trades
        n = max(self.slots, len(trades))
        self.setMinimumHeight(n * (self.SLOT_H + 6))
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        w = self.width()
        n = max(self.slots, len(self.trades))
        for i in range(n):
            r = QRect(0, i * (self.SLOT_H + 6), w, self.SLOT_H)
            t = self.trades[i] if i < len(self.trades) else None
            px.slot(p, r, selected=bool(t))
            if t:
                self._trade(p, r, t)
            else:
                p.setFont(font_vt(20))
                p.setPen(col("stone_lo"))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, "empty slot")
        p.end()

    def _trade(self, p: QPainter, r: QRect, t: dict) -> None:
        long_ = t["direction"] > 0
        p.setFont(font_vt(24))
        p.setPen(col("black"))
        p.drawText(r.left() + 10, r.top() + 22, f"{t['symbol']} {'LONG' if long_ else 'SHORT'} x{t.get('qty', 0):,.0f}")
        rv = t.get("r") or 0.0
        p.setPen(col("#1a5e0c") if rv >= 0 else col("#8a1f12"))
        p.drawText(QRect(r.left(), r.top() + 2, r.width() - 12, 24), Qt.AlignmentFlag.AlignRight, f"{rv:+.2f}R")
        stop, entry, tgt, price = t.get("stop"), t.get("entry"), t.get("target"), t.get("price")
        bar = QRect(r.left() + 10, r.top() + 32, r.width() - 20, 14)
        p.fillRect(bar.adjusted(-2, -2, 2, 2), col("black"))
        if stop and tgt and entry and stop != tgt:
            lo, hi = (stop, tgt) if long_ else (tgt, stop)
            span = hi - lo

            def xpos(v):
                f = (v - lo) / span
                f = f if long_ else 1 - f
                return bar.left() + int(max(0.0, min(1.0, f)) * bar.width())

            xe = xpos(entry)
            p.fillRect(QRect(bar.left(), bar.top(), xe - bar.left(), bar.height()), col("lava"))
            p.fillRect(QRect(xe, bar.top(), bar.right() - xe, bar.height()), col("grass"))
            p.fillRect(QRect(bar.right() - 10, bar.top(), 10, bar.height()), col("diamond"))
            if price:
                xp = xpos(price)
                p.fillRect(QRect(xp - 2, bar.top() - 4, 4, bar.height() + 8), col("text"))
                p.fillRect(QRect(xp - 1, bar.top() - 3, 2, bar.height() + 6), col("black"))
        p.setFont(font_vt(18))
        p.setPen(col("#222222"))
        p.drawText(r.left() + 10, r.bottom() - 8, f"stop {stop:,.2f}")
        p.drawText(QRect(r.left(), r.bottom() - 26, r.width(), 22), Qt.AlignmentFlag.AlignHCenter,
                   f"entry {entry:,.2f}")
        p.drawText(QRect(r.left(), r.bottom() - 26, r.width() - (40 if t.get("checkpoint") else 12), 22),
                   Qt.AlignmentFlag.AlignRight, f"target {tgt:,.2f}")
        if t.get("checkpoint"):
            p.drawPixmap(r.right() - 26, r.bottom() - 26, px.icon_pixmap("flag", 2))


# ----------------------------------------------------------------- journal
class JournalView(QTextBrowser):
    KIND_COLOR = {
        "taken": "#1a5e0c", "exit": "#3d2b14", "veto": "#8a1f12", "setup": "#6b4f2a",
        "guardian": "#5a2d82", "status": "#4a4a4a", "checkpoint": "#7a5a00", "alert": "#8a1f12",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[dict] = []
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QTextBrowser { background: transparent; color: #3d2b14; }"
                           "QScrollBar:vertical { background: #dcc48e; width: 12px; }"
                           "QScrollBar::handle:vertical { background: #6b4f2a; min-height: 24px; }")
        self.setFont(font_vt(20))

    def add(self, items: list[dict]) -> None:
        if not items:
            return
        self.entries.extend(items)
        self.entries = self.entries[-400:]
        self.render()

    def render(self) -> None:
        lines = []
        for ev in reversed(self.entries):
            c = self.KIND_COLOR.get(ev.get("kind"), "#3d2b14")
            text = html.escape(str(ev.get("text", "")))
            if ev.get("kind") == "exit":
                c = "#1a5e0c" if (ev.get("pnl") or 0) > 0 else "#8a1f12"
            sym = html.escape(ev.get("symbol") or "")
            tc = ev.get("tc")
            tc_s = f" <span style='color:#6b4f2a'>[count {tc:+.1f}]</span>" if isinstance(tc, (int, float)) else ""
            lines.append(f"<div style='margin:0 0 4px 0'><span style='color:#6b4f2a'>{et_hm(ev.get('ts'))}</span> "
                         f"<b style='color:#3d2b14'>{sym}</b> <span style='color:{c}'>{text}</span>{tc_s}</div>")
        if not lines:
            lines = ["<div style='color:#6b4f2a'>The journal is empty. Setups, vetoes and trades will be written "
                     "here as the bot plays the level.</div>"]
        self.setHtml("".join(lines))


# ------------------------------------------------------------ treasure map
class TreasureMap(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.points: list[tuple[float, float]] = []
        self.marks: list[tuple[float, float, bool]] = []
        self.caption = "today"
        self.setMinimumHeight(170)

    def set_path(self, points, marks, caption: str) -> None:
        self.points, self.marks, self.caption = points, marks, caption
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        r = self.rect()
        px.parchment(p, r, 9)
        px.title(p, 14, 23, f"TREASURE MAP: {self.caption.upper()}", 10, "ink", "parch2")
        self._compass(p, r.right() - 54, r.top() + 34)
        area = QRect(r.left() + 46, r.top() + 40, r.width() - 120, r.height() - 64)
        pts = self.points
        p.setFont(font_vt(18))
        if len(pts) < 2:
            p.setPen(col("ink_soft"))
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "The path appears once the bot starts trading.")
            p.end()
            return
        xs = [a for a, _ in pts]
        ys = [b for _, b in pts]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if y1 - y0 < 1:
            y0, y1 = y0 - 1, y1 + 1
        pad = (y1 - y0) * 0.12
        y0, y1 = y0 - pad, y1 + pad

        def map_pt(t, v):
            fx = (t - x0) / (x1 - x0) if x1 > x0 else 0.5
            fy = (v - y0) / (y1 - y0)
            return QPoint(area.left() + int(fx * area.width()), area.bottom() - int(fy * area.height()))

        base = pts[0][1]
        yb = map_pt(x0, base).y()
        pen = QPen(col("ink_soft"), 1, Qt.PenStyle.DotLine)
        p.setPen(pen)
        p.drawLine(area.left(), yb, area.right(), yb)
        pen = QPen(col("ink"), 3, Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        prev = map_pt(*pts[0])
        for t, v in pts[1:]:
            cur = map_pt(t, v)
            p.drawLine(prev.x(), prev.y(), cur.x(), prev.y())        # pixel-style steps
            p.drawLine(cur.x(), prev.y(), cur.x(), cur.y())
            prev = cur
        p.setFont(font_px(12))
        for t, v, win in self.marks:
            q = map_pt(t, v)
            p.setPen(col("#b0281a") if win else col("ink"))
            p.drawText(q.x() - 6, q.y() + 6, "X" if win else "*")
        p.setFont(font_vt(18))
        p.setPen(col("ink"))
        p.drawText(r.left() + 12, area.top() + 10, f"${y1 - pad:,.0f}")
        p.drawText(r.left() + 12, area.bottom(), f"${y0 + pad:,.0f}")
        last = pts[-1][1]
        p.setPen(col("#1a5e0c") if last >= base else col("#8a1f12"))
        p.drawText(area.right() - 120, r.bottom() - 10, f"now ${last:,.2f}")
        p.end()

    @staticmethod
    def _compass(p: QPainter, cx: int, cy: int) -> None:
        s = 3
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(-4, 5):
            p.fillRect(cx + i * s, cy, s, s, col("ink_soft"))
            p.fillRect(cx, cy + i * s, s, s, col("ink_soft"))
        for i in range(1, 5):
            p.fillRect(cx, cy - i * s, s, s, col("#b0281a"))
        p.setFont(font_px(8))
        p.setPen(col("ink"))
        p.drawText(cx - 3, cy - 16, "N")


# --------------------------------------------------------------- scoreboard
class ScoreView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.lines: list[tuple[str, str, str]] = []
        self.setMinimumHeight(150)

    def set_lines(self, lines: list[tuple[str, str, str]]) -> None:
        self.lines = lines
        self.setMinimumHeight(24 * len(lines) + 6)
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        y = 18
        for label, value, color in self.lines:
            if label.startswith("#"):
                p.setFont(font_px(8))
                p.setPen(col("gold"))
                p.drawText(0, y, label[1:])
            else:
                p.setFont(font_vt(20))
                p.setPen(col("muted"))
                p.drawText(0, y, label)
                p.setPen(col(color))
                p.drawText(QRect(0, y - 18, self.width() - 4, 24), Qt.AlignmentFlag.AlignRight, value)
            y += 24
        p.end()


# ------------------------------------------------------------------ hotbar
class HotbarButton(QAbstractButton):
    def __init__(self, label: str, icon: str, tnt: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.label = label
        self.pix = px.icon_pixmap(icon, 4)
        self.tnt = tnt
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(QSize(118, 74))
        self.setToolTip(label)

    def enterEvent(self, e) -> None:
        self.update()

    def leaveEvent(self, e) -> None:
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        r = self.rect()
        hover = self.underMouse()
        if self.tnt:
            px.bevel(p, r, "redstone", "#e2554f", "#7a1c18", "black", 3)
        else:
            px.slot(p, r, selected=hover)
        if self.isDown():
            p.fillRect(r.adjusted(3, 3, -3, -3), col("#00000040"))
        if self.tnt:
            b = QRect((r.width() - 40) // 2, 8, 40, 34)
            p.fillRect(b, col("black"))
            p.fillRect(b.adjusted(2, 2, -2, -2), col("#d23b2f"))
            for i in range(0, 36, 6):
                p.fillRect(b.left() + 2 + i, b.top() + 2, 3, 30, col("#a8261c"))
            band = QRect(b.left() + 2, b.top() + 11, b.width() - 4, 12)
            p.fillRect(band, col("text"))
            p.setFont(font_px(8))
            p.setPen(col("black"))
            p.drawText(band, Qt.AlignmentFlag.AlignCenter, "TNT")
            p.fillRect(QRect(b.center().x() - 1, b.top() - 6, 3, 6), col("stone_hi"))
        else:
            p.drawPixmap((r.width() - self.pix.width()) // 2, 8, self.pix)
        p.setFont(font_px(9))
        p.setPen(col("text") if self.tnt else col("black"))
        p.drawText(QRect(0, r.height() - 24, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.label)
        if hover and self.tnt:
            p.setPen(col("gold"))
            p.drawRect(r.adjusted(1, 1, -2, -2))
        p.end()


class Footer(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(28)
        self.left = ""
        self.right = ""
        self.toast = ""

    def set_text(self, left: str, right: str) -> None:
        self.left, self.right = left, right
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        r = self.rect()
        p.drawPixmap(0, 0, px.texture("dirt", r.width(), r.height(), 6))
        p.setFont(font_vt(19))
        p.setPen(col("parch"))
        p.drawText(r.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.toast or self.left)
        p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.right)
        p.end()


def countdown(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def fmt_r(v: float | None) -> str:
    return "--" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.2f}R"
