"""8-bit sound effects synthesised in code (square waves), played with Qt."""

from __future__ import annotations

import struct
import tempfile
import wave
from pathlib import Path

RATE = 22050

TUNES = {
    "taken": [(523, 0.07), (659, 0.07), (784, 0.10)],                 # rising arpeggio
    "win": [(988, 0.06), (1319, 0.16)],                               # coin
    "loss": [(330, 0.09), (247, 0.09), (196, 0.18)],                  # falling
    "panic": [(110, 0.12), (90, 0.12), (70, 0.30)],                   # rumble
    "click": [(1200, 0.02)],
}


def _square(freq: float, dur: float, vol: float = 0.25) -> bytes:
    n = int(RATE * dur)
    out = bytearray()
    period = RATE / freq
    for i in range(n):
        env = min(1.0, (n - i) / (RATE * 0.02))                       # quick release
        v = vol * env * (1 if (i % period) < period / 2 else -1)
        out += struct.pack("<h", int(v * 32767))
    return bytes(out)


def write_tunes(folder: Path | None = None) -> dict[str, Path]:
    folder = Path(folder or tempfile.mkdtemp(prefix="dashcount-sfx-"))
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, notes in TUNES.items():
        p = folder / f"{name}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(_square(f, d) for f, d in notes))
        paths[name] = p
    return paths


class Sounds:
    def __init__(self) -> None:
        self.enabled = False
        self._fx = {}
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

            for name, path in write_tunes().items():
                fx = QSoundEffect()
                fx.setSource(QUrl.fromLocalFile(str(path)))
                fx.setVolume(0.5)
                self._fx[name] = fx
        except Exception:
            self._fx = {}

    def play(self, name: str) -> None:
        if self.enabled and name in self._fx:
            self._fx[name].play()
