"""Flight recorder: every attempt, order, trade, veto call and guardian event,
written as Parquet so it loads straight into pandas / DuckDB for training."""

from __future__ import annotations

import datetime as dt
import json
import math
import threading
from pathlib import Path

import pandas as pd

TABLES = ("attempts", "outcomes", "orders", "trades", "veto_calls", "guardian_events", "daily")


def _clean(v):
    if isinstance(v, (dict, list, tuple, set)):
        return json.dumps(v if not isinstance(v, set) else sorted(v), default=str)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


class Recorder:
    def __init__(self, root: Path, run_id: str, version: str, enabled: bool = True) -> None:
        self.root = Path(root) / run_id
        self.run_id = run_id
        self.version = version
        self.enabled = enabled
        self._rows: dict[str, list[dict]] = {t: [] for t in TABLES}
        self._lock = threading.Lock()
        self._part = 0
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def write_meta(self, meta: dict) -> None:
        if self.enabled:
            (self.root / "run.json").write_text(json.dumps(meta, indent=2, default=str))

    def add(self, table: str, row: dict) -> None:
        if not self.enabled:
            return
        r = {"run_id": self.run_id, "version": self.version}
        r.update({k: _clean(v) for k, v in row.items()})
        with self._lock:
            self._rows.setdefault(table, []).append(r)

    def flush(self, tag: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            pending = {t: rows for t, rows in self._rows.items() if rows}
            self._rows = {t: [] for t in TABLES}
            self._part += 1
            part = self._part
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for table, rows in pending.items():
            d = self.root / table
            d.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame(rows)
            for col in df.columns:            # mixed object columns -> strings
                if df[col].dtype == object:
                    kinds = {type(x) for x in df[col].dropna()}
                    if len(kinds) > 1:
                        df[col] = df[col].map(lambda x: None if x is None else str(x))
            df.to_parquet(d / f"{tag or 'part'}-{stamp}-{part:04d}.parquet", index=False)


def read_table(run_dir: Path, table: str) -> pd.DataFrame:
    files = sorted((Path(run_dir) / table).glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
