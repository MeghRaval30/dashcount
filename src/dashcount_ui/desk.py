"""The DESK screen: risk meters, orders and fills, system health and alerts.

Each panel body paints from plain data. A bad value shows "error: ..." in that
panel only; the window never crashes.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGridLayout, QWidget

from . import pixel as px
from .alerts import Alert
from .broker_feed import FeedState
from .datasource import ago
from .health import HealthRow
from .pixel import col, font_px, font_vt
from .reconcile import BlotterRow
from .risk import Meter
from .widgets import ParchmentPanel, PixelPanel

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

LEVEL_COLOR = {"ok": "emerald", "warn": "gold", "danger": "redstone", "stale": "stone_lo", "idle": "stone_lo"}
STATUS_COLOR = {"filled": "good", "rejected": "bad", "canceled": "muted", "expired": "muted", "replaced": "muted"}
ALERT_INK = {"info": "#3d2b14", "warn": "#7a5a00", "danger": "#8a1f12"}


@dataclass
class DeskView:
    meters: list[Meter]
    stale_text: str
    rows: list[BlotterRow]
    summary: dict
    feed: FeedState | None
    banner: list[str]
    health: list[HealthRow]
    alerts: list[Alert]


@dataclass(frozen=True)
class PanelError:
    """Stands in for a panel's data when building it failed: that panel shows the error."""
    message: str


def meter_step(avail: int, n: int) -> int:
    """Height per meter: roomy when there is space, compact (label and value on one line)
    when the panel is short, so every meter stays visible."""
    return max(36, min(58, avail // max(n, 1)))


def visible_rows(rows: list, fit: int, is_leg) -> list:
    """The newest rows that fit, starting at an entry so no stop or target shows without
    its order (falls back to the plain tail when not even one whole group fits)."""
    tail = rows[-fit:] if fit > 0 else []
    start = next((i for i, r in enumerate(tail) if not is_leg(r)), None)
    return tail[start:] if start is not None else tail


def _hm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=ET).strftime("%H:%M") if ts else "--:--"


class _Body(QWidget):
    """A panel body. prepare() checks the data; paint errors and bad data show in the panel."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.error = ""
        self.data: dict = {}
        self.setMinimumHeight(120)

    def set(self, **data) -> None:
        self.error = ""
        failed = next((v for v in data.values() if isinstance(v, PanelError)), None)
        if failed is not None:
            self.error = failed.message
            self.update()
            return
        try:
            self.data = self.prepare(**data)
        except Exception as e:                 # noqa: BLE001 - a panel must never take the window down
            log.exception("%s: bad data", self.name)
            self.error = str(e) or type(e).__name__
        self.update()

    def prepare(self, **data) -> dict:
        return data

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        try:
            if not self.error:
                self.draw(p)
        except Exception as ex:                 # noqa: BLE001
            log.exception("%s: paint failed", self.name)
            self.error = str(ex) or type(ex).__name__
        if self.error:
            p.setFont(font_vt(20))
            p.setPen(col("bad"))
            p.drawText(self.rect().adjusted(6, 6, -6, -6), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       f"error: {self.error}")
        p.end()

    def draw(self, p: QPainter) -> None:  # pragma: no cover - subclasses
        pass


# ----------------------------------------------------------------- risk meters
class RiskBody(_Body):
    BLOCKS = 20

    def prepare(self, meters: list[Meter], stale: str) -> dict:
        return {"meters": [(m.key, m.label, float(m.frac), m.level, m.text, m.used, m.limit) for m in meters],
                "stale": stale}

    def draw(self, p: QPainter) -> None:
        w = self.width()
        y = 4
        if self.data["stale"]:
            p.setFont(font_vt(18))
            p.setPen(col("muted"))
            p.drawText(0, y + 14, self.data["stale"])
            y += 22
        heart = px.icon_pixmap("heart", 2)
        step = meter_step(self.height() - y, len(self.data["meters"]))
        compact = step < 58
        for key, label, frac, level, text, used, limit in self.data["meters"]:
            p.setFont(font_px(8))
            p.setPen(col("gold") if level != "stale" else col("muted"))
            p.drawText(0, y + 10, label)
            bar_y = y + 16
            if compact:                       # value on the label's line, to the right
                p.setFont(font_vt(18))
                p.setPen(col("text") if level != "stale" else col("muted"))
                p.drawText(QRect(0, y - 4, w, 18), Qt.AlignmentFlag.AlignRight,
                           p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(w * 0.65)))
            if key == "streak":
                lost = int(round(used))
                for i in range(int(round(limit))):
                    x = i * (heart.width() + 6)
                    p.setOpacity(0.25 if i >= int(round(limit)) - lost else 1.0)
                    p.drawPixmap(x, bar_y, heart)
                p.setOpacity(1.0)
            else:
                bw = max(4, (w - 4) // self.BLOCKS - 2)
                filled = min(self.BLOCKS, int(round(frac * self.BLOCKS)))
                for i in range(self.BLOCKS):
                    x = i * (bw + 2)
                    p.fillRect(QRect(x, bar_y, bw, 14), col("black"))
                    c = LEVEL_COLOR[level] if i < filled else "deepslate_lo"
                    p.fillRect(QRect(x + 1, bar_y + 1, bw - 2, 12), col(c))
            if not compact:
                p.setFont(font_vt(18))
                p.setPen(col("text") if level != "stale" else col("muted"))
                p.drawText(0, bar_y + 32, text)
            y += step


# ------------------------------------------------------------- orders & fills
class BlotterBody(_Body):
    COLS = (("TIME", 0.0), ("STOCK", 0.08), ("SIDE", 0.19), ("ROLE", 0.27), ("QTY", 0.37), ("PRICE", 0.47),
            ("STATUS", 0.58), ("FILL", 0.72), ("SLIP", 0.83))
    ROW = 22

    def prepare(self, rows: list[BlotterRow], summary: dict, feed: FeedState | None, banner: list[str]) -> dict:
        return {"rows": [(r.ts, r.symbol, r.side, r.role, r.qty, r.filled_qty, r.price, r.status, r.fill, r.slip,
                          r.slip_r, r.parent_id is not None) for r in rows],
                "summary": dict(summary or {}), "feed": feed, "banner": [str(b) for b in banner]}

    def draw(self, p: QPainter) -> None:
        w, h = self.width(), self.height()
        d = self.data
        y = 0
        for msg in d["banner"]:
            p.fillRect(QRect(0, y, w, 22), col("redstone"))
            p.setFont(font_vt(19))
            p.setPen(col("text"))
            p.drawText(QRect(8, y, w - 16, 22), Qt.AlignmentFlag.AlignVCenter, f"MISMATCH  {msg}")
            y += 26
        feed = d["feed"]
        empty = None
        if feed is None:
            empty = "Alpaca keys not set (add them to .env)"
        elif not feed.ok and feed.last_ok_ts is None and feed.errors_10min == 0:
            empty = "Connecting to Alpaca..."
        elif not feed.ok:
            last = f"{ago(time.time() - feed.last_ok_ts)} ago" if feed.last_ok_ts else "never"
            empty = f"Alpaca not reachable: last good data {last}" if not d["rows"] else None
        if empty is None and not d["rows"]:
            empty = "No orders today"
        s = d["summary"]
        if s:
            slip = (f"entry slip {s['entry_slip_bps']:+.1f} bps ({s['entry_slip_r']:+.2f}R)"
                    if s.get("entry_slip_bps") is not None else "entry slip --")
            p.setFont(font_vt(19))
            p.setPen(col("parch"))
            p.drawText(0, y + 15, f"orders {s.get('orders', 0)}   fills {s.get('fills', 0)}   {slip}")
            if s.get("rejected"):
                p.setPen(col("bad"))
                p.drawText(QRect(0, y, w, 20), Qt.AlignmentFlag.AlignRight, f"REJECTED {s['rejected']}")
            y += 24
        if empty:
            p.setFont(font_vt(22))
            p.setPen(col("muted"))
            p.drawText(QRect(0, y, w, h - y), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, empty)
            return
        p.setFont(font_px(7))
        p.setPen(col("muted"))
        for name, fx in self.COLS:
            p.drawText(int(fx * w), y + 10, name)
        y += 16
        fit = max(1, (h - y) // self.ROW)
        p.setFont(font_vt(18))
        for ts, sym, side, role, qty, filled, price, status, fill, slip, slip_r, is_leg in \
                visible_rows(d["rows"], fit, lambda r: r[-1]):
            ind = 24 if is_leg else 0
            vals = (_hm(ts), sym, side, role, f"{filled:g}/{qty:g}" if role == "ENTRY" else f"{qty:g}",
                    f"{price:,.2f}" if price is not None else "mkt", status.replace("_", " "),
                    f"{fill:,.2f}" if fill is not None else "--",
                    f"{slip:+.2f} ({slip_r:+.2f}R)" if slip is not None and slip_r is not None else "--")
            for i, ((_, fx), v) in enumerate(zip(self.COLS, vals)):
                x = int(fx * w) + (ind if i in (1, 2, 3) else 0)
                if i == 6:
                    p.setPen(col(STATUS_COLOR.get(status, "gold")))
                elif i == 8 and slip is not None:
                    p.setPen(col("bad") if slip > 0 else col("good"))
                else:
                    p.setPen(col("text") if not is_leg else col("stone_hi"))
                p.drawText(x, y + 15, v)
            y += self.ROW


# --------------------------------------------------------------- system health
class HealthBody(_Body):
    def prepare(self, rows: list[HealthRow]) -> dict:
        return {"rows": [(r.label, r.level, r.text) for r in rows]}

    def draw(self, p: QPainter) -> None:
        y = 4
        for label, level, text in self.data["rows"]:
            px.bevel(p, QRect(0, y + 2, 14, 14), LEVEL_COLOR[level], LEVEL_COLOR[level], "black", "black", 1)
            p.setFont(font_px(8))
            p.setPen(col("parch"))
            p.drawText(24, y + 14, label)
            p.setFont(font_vt(19))
            p.setPen(col("muted") if level == "idle" else col("text"))
            p.drawText(QRect(150, y, self.width() - 150, 20), Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, self.width() - 150))
            y += 26


# ---------------------------------------------------------------------- alerts
class AlertsBody(_Body):
    def prepare(self, alerts: list[Alert]) -> dict:
        return {"alerts": [(a.ts, a.level, a.text) for a in alerts]}

    def draw(self, p: QPainter) -> None:
        w = self.width()
        alerts = self.data["alerts"]
        p.setFont(font_vt(19))
        if not alerts:
            p.setPen(col("ink_soft"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       "No alerts yet. Trades, limits and problems will show up here.")
            return
        y = 2
        for ts, level, text in alerts:
            if y > self.height() - 20:
                break
            p.setPen(col("ink_soft"))
            p.drawText(0, y + 16, _hm(ts))
            p.fillRect(QRect(48, y + 5, 10, 10), col(LEVEL_COLOR.get(level, "stone_lo") if level != "info" else "lapis"))
            p.setPen(col(ALERT_INK.get(level, "#3d2b14")))
            p.drawText(66, y + 16, p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, w - 70))
            y += 22


# ---------------------------------------------------------------------- screen
class DeskScreen(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 10, 10, 6)
        grid.setSpacing(10)
        self.risk, self.blotter = RiskBody("RISK METERS"), BlotterBody("ORDERS & FILLS")
        self.health, self.alerts = HealthBody("SYSTEM HEALTH"), AlertsBody("ALERTS")
        for (title, icon), body, r, c in ((("RISK METERS", "shield"), self.risk, 0, 0),
                                          (("ORDERS & FILLS", "book"), self.blotter, 0, 1),
                                          (("SYSTEM HEALTH", "compass"), self.health, 1, 0)):
            panel = PixelPanel(title, icon)
            panel.body.addWidget(body)
            grid.addWidget(panel, r, c)
        ap = ParchmentPanel("ALERTS")
        ap.body.addWidget(self.alerts)
        grid.addWidget(ap, 1, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 7)
        grid.setRowStretch(0, 6)
        grid.setRowStretch(1, 5)
        self.view: DeskView | None = None

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, px.texture("stone", self.width(), self.height(), 12))
        p.end()

    def show_view(self, v: DeskView) -> None:
        self.view = v
        self.risk.set(meters=v.meters, stale=v.stale_text)
        self.blotter.set(rows=v.rows, summary=v.summary, feed=v.feed, banner=v.banner)
        self.health.set(rows=v.health)
        self.alerts.set(alerts=v.alerts)

    def panel_errors(self) -> list[str]:
        return [b.name for b in (self.risk, self.blotter, self.health, self.alerts) if b.error]
