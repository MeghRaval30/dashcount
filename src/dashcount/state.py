"""Small JSON state files shared between the bot and the Guardian (live)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import data_dir


def state_dir() -> Path:
    d = data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def guardian_path() -> Path:
    return state_dir() / "guardian.json"


def heartbeat_path() -> Path:
    return state_dir() / "heartbeat.json"


def bot_positions_path() -> Path:
    return state_dir() / "bot_positions.json"


def veto_score_path() -> Path:
    return state_dir() / "veto_score.json"


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def beat(extra: dict | None = None) -> None:
    write_json(heartbeat_path(), {"ts": time.time(), **(extra or {})})


def heartbeat_age() -> float | None:
    hb = read_json(heartbeat_path())
    return time.time() - hb["ts"] if hb and "ts" in hb else None
