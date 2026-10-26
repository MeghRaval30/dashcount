"""Pixel-art toolkit: palette, fonts, bevelled panels, block textures, parchment
and tiny bitmap icons. Everything is drawn in code (no game assets)."""

from __future__ import annotations

import random
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPixmap, QPolygon

# ------------------------------------------------------------------ palette
C = {
    "bg": "#1b1b1f",
    "deepslate": "#34343a", "deepslate_hi": "#52525a", "deepslate_lo": "#1f1f24",
    "stone": "#7d7d7d", "stone_hi": "#a8a8a8", "stone_lo": "#4a4a4a",
    "slot": "#8b8b8b", "slot_hi": "#d6d6d6", "slot_lo": "#373737",
    "grass": "#5d9c2f", "grass2": "#4a8a22", "dirt": "#8a5a2b", "dirt2": "#6f4720",
    "parch": "#e8d5a6", "parch2": "#dcc48e", "ink": "#3d2b14", "ink_soft": "#6b4f2a",
    "gold": "#f6d55c", "gold_lo": "#b8922a", "diamond": "#4fd1d9", "emerald": "#3ec46d",
    "lava": "#e0533d", "lava_lo": "#a8341e", "redstone": "#c4302b", "lapis": "#3c6dd8",
    "amethyst": "#9b6bd8", "text": "#f2f2f2", "muted": "#a3a3a8", "black": "#0b0b0d",
    "good": "#7ed957", "bad": "#ff7b6b", "warn": "#f6b44c",
}


def col(name: str) -> QColor:
    return QColor(C.get(name, name))


# -------------------------------------------------------------------- fonts
FONT_DIR = (Path(getattr(sys, "_MEIPASS", "")) / "dashcount_ui" / "assets" / "fonts"
            if getattr(sys, "frozen", False) else Path(__file__).parent / "assets" / "fonts")
_loaded = False


def load_fonts() -> None:
    global _loaded
    if _loaded:
        return
    for f in ("PressStart2P-Regular.ttf", "VT323-Regular.ttf"):
        p = FONT_DIR / f
        if p.exists():
            QFontDatabase.addApplicationFont(str(p))
    _loaded = True


def font_px(size: int) -> QFont:
    """Blocky heading font (Press Start 2P), crisp (no antialiasing)."""
    f = QFont("Press Start 2P")
    f.setPixelSize(size)
    f.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    return f


def font_vt(size: int) -> QFont:
    """Terminal body font (VT323)."""
    f = QFont("VT323")
    f.setPixelSize(size)
    f.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    return f


# ---------------------------------------------------------------- painting
def bevel(p: QPainter, r: QRect, base: str, hi: str, lo: str, border: str | None = "black", w: int = 3) -> None:
    """Minecraft-style bevelled box: light top-left, dark bottom-right."""
    p.fillRect(r, col(base))
    p.fillRect(QRect(r.left(), r.top(), r.width(), w), col(hi))
    p.fillRect(QRect(r.left(), r.top(), w, r.height()), col(hi))
    p.fillRect(QRect(r.left(), r.bottom() - w + 1, r.width(), w), col(lo))
    p.fillRect(QRect(r.right() - w + 1, r.top(), w, r.height()), col(lo))
    if border:
        p.setPen(col(border))
        p.drawRect(r.adjusted(0, 0, -1, -1))


def slot(p: QPainter, r: QRect, selected: bool = False) -> None:
    """Inventory slot: inset (dark top-left, light bottom-right)."""
    bevel(p, r, "slot", "slot_lo", "slot_hi", border=None, w=2)
    if selected:
        p.setPen(col("text"))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        p.drawRect(r.adjusted(1, 1, -2, -2))


@lru_cache(maxsize=64)
def texture(kind: str, w: int, h: int, block: int = 6, seed: int = 7) -> QPixmap:
    """Deterministic noisy block texture: grass, dirt, stone, deepslate, parchment."""
    palettes = {
        "grass": ["#5d9c2f", "#4a8a22", "#66a835", "#538f28"],
        "dirt": ["#8a5a2b", "#79501f", "#6f4720", "#946234"],
        "stone": ["#2b2b30", "#29292e", "#2f2f35", "#26262b"],
        "deepslate": ["#34343a", "#323238", "#37373e", "#303036"],
        "parch": ["#e8d5a6", "#e4cf9c", "#ebd9ad", "#dfc993"],
    }
    pal = palettes[kind]
    img = QPixmap(max(w, 1), max(h, 1))
    img.fill(QColor(pal[0]))
    p = QPainter(img)
    rnd = random.Random(f"{kind}{seed}{w}{h}")
    for y in range(0, h, block):
        for x in range(0, w, block):
            p.fillRect(x, y, block, block, QColor(rnd.choice(pal)))
    if kind == "parch":
        for _ in range(w * h // 900):
            x, y = rnd.randrange(w), rnd.randrange(h)
            p.fillRect(x, y, 2, 2, QColor("#cdb37b"))
    p.end()
    return img


def panel(p: QPainter, r: QRect) -> None:
    """Dark deepslate GUI panel with bevel."""
    p.drawPixmap(r.topLeft(), texture("deepslate", r.width(), r.height(), 8))
    p.setPen(Qt.PenStyle.NoPen)
    p.fillRect(QRect(r.left(), r.top(), r.width(), 3), col("deepslate_hi"))
    p.fillRect(QRect(r.left(), r.top(), 3, r.height()), col("deepslate_hi"))
    p.fillRect(QRect(r.left(), r.bottom() - 2, r.width(), 3), col("deepslate_lo"))
    p.fillRect(QRect(r.right() - 2, r.top(), 3, r.height()), col("deepslate_lo"))
    p.setPen(col("black"))
    p.drawRect(r.adjusted(0, 0, -1, -1))


def parchment(p: QPainter, r: QRect, seed: int = 3) -> None:
    """Old-map parchment with a ragged, inked edge."""
    p.drawPixmap(r.topLeft(), texture("parch", r.width(), r.height(), 6, seed))
    rnd = random.Random(seed)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col("bg"))
    step = 6
    for x in range(r.left(), r.right(), step):           # ragged top and bottom edges
        if rnd.random() < 0.45:
            p.drawRect(x, r.top(), step, rnd.choice((2, 4)))
        if rnd.random() < 0.45:
            p.drawRect(x, r.bottom() - rnd.choice((1, 3)), step, 4)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(col("ink_soft"))
    p.drawRect(r.adjusted(2, 2, -3, -3))


def grass_strip(p: QPainter, r: QRect) -> None:
    gh = max(6, r.height() // 2)
    p.drawPixmap(r.left(), r.top(), texture("grass", r.width(), gh, 6))
    p.drawPixmap(r.left(), r.top() + gh, texture("dirt", r.width(), r.height() - gh, 6))
    rnd = random.Random(11)
    for x in range(r.left(), r.right(), 6):                # grass hanging over the dirt
        if rnd.random() < 0.5:
            p.fillRect(x, r.top() + gh, 6, 6, col("grass2"))


def title(p: QPainter, x: int, y: int, text: str, size: int = 10, color: str = "gold", shadow: str = "ink") -> None:
    p.setFont(font_px(size))
    p.setPen(col(shadow))
    p.drawText(x + 2, y + 2, text)
    p.setPen(col(color))
    p.drawText(x, y, text)


# ------------------------------------------------------------------- icons
# tiny bitmaps: each char is a pixel, '.' transparent, letters map to colours
ICONS = {
    "pickaxe": ([
        "....kkkkkk......",
        "..kkddddddkk....",
        ".kddkkkkkkddk...",
        ".kdk......kddk..",
        ".kk.......kwkdk.",
        "..........kw.kdk",
        ".........kwk..kk",
        "........kwk.....",
        ".......kwk......",
        "......kwk.......",
        ".....kwk........",
        "....kwk.........",
        "...kwk..........",
        "..kwk...........",
        ".kwk............",
        ".kk.............",
    ], {"k": "#0b0b0d", "d": "#4fd1d9", "w": "#8a5a2b"}),
    "play": ([
        ".k.......",
        ".kgk.....",
        ".kggk....",
        ".kgggk...",
        ".kggggk..",
        ".kgggk...",
        ".kggk....",
        ".kgk.....",
        ".k.......",
    ], {"k": "#0b0b0d", "g": "#3ec46d"}),
    "stop": ([
        "kkkkkkkk",
        "krrrrrrk",
        "krrrrrrk",
        "krrrrrrk",
        "krrrrrrk",
        "krrrrrrk",
        "krrrrrrk",
        "kkkkkkkk",
    ], {"k": "#0b0b0d", "r": "#a8341e"}),
    "tnt": ([
        "....s...",
        "...s....",
        "rrrrrrrr",
        "rwwwwwwr",
        "wkkwkkkw",
        "wwkwwkww",
        "rwwwwwwr",
        "rrrrrrrr",
    ], {"r": "#c4302b", "w": "#f2f2f2", "k": "#0b0b0d", "s": "#a3a3a8"}),
    "shield": ([
        "kkkkkkkk",
        "kllllllk",
        "kllyllk.",
        "klyyylk.",
        ".kllllk.",
        ".kllllk.",
        "..kllk..",
        "...kk...",
    ], {"k": "#0b0b0d", "l": "#3c6dd8", "y": "#f6d55c"}),
    "compass": ([
        "..kkkk..",
        ".kppppk.",
        "kpprppk.",
        "kpprrppk",
        "kpwwpppk",
        "kpwppppk",
        ".kppppk.",
        "..kkkk..",
    ], {"k": "#0b0b0d", "p": "#e8d5a6", "r": "#c4302b", "w": "#4a4a4a"}),
    "book": ([
        "kkkkkkk.",
        "kbbbbbbk",
        "kbppppbk",
        "kbppppbk",
        "kbppppbk",
        "kbppppbk",
        "kbbbbbbk",
        "kkkkkkk.",
    ], {"k": "#0b0b0d", "b": "#8a5a2b", "p": "#e8d5a6"}),
    "sword": ([
        "......ss",
        ".....sws",
        "....sws.",
        "...sws..",
        "k.sws...",
        ".kws....",
        "..kk....",
        ".k..k...",
    ], {"s": "#4fd1d9", "w": "#f2f2f2", "k": "#8a5a2b"}),
    "spike": ([
        "...k....",
        "..kgk...",
        "..kgk...",
        ".kgggk..",
        ".kgggk..",
        "kgggggk.",
        "kkkkkkk.",
        "........",
    ], {"k": "#0b0b0d", "g": "#a8a8a8"}),
    "flag": ([
        "kggggg..",
        "kgggggg.",
        "kggggg..",
        "k.......",
        "k.......",
        "k.......",
        "k.......",
        "k.......",
    ], {"k": "#0b0b0d", "g": "#f6d55c"}),
    "speaker": ([
        "....k...",
        "...kk..w",
        "kkkkk.w.",
        "kwwwk.w.",
        "kwwwk.w.",
        "kkkkk.w.",
        "...kk..w",
        "....k...",
    ], {"k": "#0b0b0d", "w": "#f2f2f2"}),
    "heart": ([
        ".kk...kk.",
        "krrk.krrk",
        "krwrkrrrk",
        "krrrrrrrk",
        ".krrrrrk.",
        "..krrrk..",
        "...krk...",
        "....k....",
    ], {"k": "#0b0b0d", "r": "#c4302b", "w": "#f2f2f2"}),
}


def icon_pixmap(name: str, scale: int = 3) -> QPixmap:
    rows, pal = ICONS[name]
    h, w = len(rows), max(len(r) for r in rows)
    img = QImage(w * scale, h * scale, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "." and ch in pal:
                p.fillRect(x * scale, y * scale, scale, scale, QColor(pal[ch]))
    p.end()
    return QPixmap.fromImage(img)


def app_icon() -> QIcon:
    ic = QIcon()
    for s in (2, 3, 4, 6, 8, 16):
        ic.addPixmap(icon_pixmap("pickaxe", s))
    return ic


def save_ico(path: Path) -> None:
    """Write a multi-size .ico for the exe and the desktop shortcut."""
    from PySide6.QtGui import QImageWriter

    img = icon_pixmap("pickaxe", 16).toImage()               # 192 px
    canvas = QImage(256, 256, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.drawImage(32, 32, img)
    p.end()
    w = QImageWriter(str(path), b"ico")
    if not w.write(canvas):
        canvas.save(str(path.with_suffix(".png")))


def triangle(p: QPainter, x: int, y: int, size: int, up: bool, color: str) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col(color))
    if up:
        pts = [QPoint(x, y + size), QPoint(x + size, y + size), QPoint(x + size // 2, y)]
    else:
        pts = [QPoint(x, y), QPoint(x + size, y), QPoint(x + size // 2, y + size)]
    p.drawPolygon(QPolygon(pts))
    p.setBrush(Qt.BrushStyle.NoBrush)
