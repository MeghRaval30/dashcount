"""Builders shared by practice and live: the veto chain and the attempt memory."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .config import Config, data_dir
from .recorder import read_table
from .veto import VetoChain
from .veto.memory import FEATURES, AttemptMemory, MemoryItem

log = logging.getLogger(__name__)

VETO_CHOICES = ("full", "kronos", "finr1", "none")


def records_root() -> Path:
    return data_dir() / "records"


def make_chain(cfg: Config, vetoes: str = "full", preload: bool = True) -> VetoChain | None:
    if vetoes == "none":
        return None
    from .veto.finr1 import FinR1Veto
    from .veto.kronos_veto import KronosVeto

    kronos = KronosVeto(cfg.veto.kronos) if vetoes in ("full", "kronos") else None
    if kronos is not None and preload:
        _ = kronos.predictor            # load the weights now, not inside a timed decision
    finr1 = FinR1Veto(cfg.veto.finr1) if vetoes in ("full", "finr1") else None
    if finr1 is not None and not finr1.available():
        log.warning("Fin-R1 is not available in Ollama (%s); it will answer 'unavailable' -> trades skipped. "
                    "Start Ollama or use --vetoes kronos.", cfg.veto.finr1.model)
    elif finr1 is not None and preload:
        finr1.warm_up()                 # load it into GPU memory before the first decision
    return VetoChain(cfg.veto, kronos, finr1, cache_dir=data_dir() / "cache" / "veto")


def load_memory(before_ts: float | None = None, exclude_runs: tuple[str, ...] = (), runs: list[Path] | None = None) -> AttemptMemory:
    """Resolved attempts from recorded runs (practice and live) as memory."""
    mem = AttemptMemory()
    root = records_root()
    if not root.exists():
        return mem
    for run in runs or sorted(p for p in root.iterdir() if p.is_dir()):
        if run.name in exclude_runs:
            continue
        att = read_table(run, "attempts")
        out = read_table(run, "outcomes")
        if att.empty or out.empty:
            continue
        df = att.merge(out[["attempt_id", "r", "exit_reason", "is_ghost", "resolved_ts"]], on="attempt_id", how="inner")
        if before_ts is not None:
            df = df[df["resolved_ts"] < before_ts]
        for row in df.itertuples(index=False):
            feats = {k: _num(getattr(row, k, None)) for k in FEATURES}
            mem.add(MemoryItem(row.pad, int(row.direction), feats, float(row.resolved_ts), float(row.r or 0.0),
                               str(row.exit_reason), bool(row.is_ghost), str(row.action)))
    log.info("memory: %d past attempts", len(mem))
    return mem


def _num(x):
    try:
        if x is None or pd.isna(x):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None
