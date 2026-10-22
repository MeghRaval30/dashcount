"""Memory of past attempts ("the friend who's played this level before").

Holds resolved attempts (real trades and ghosts) and finds the most similar
ones to a new candidate. Point-in-time: only attempts whose outcome was known
before `now` are returned.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np

FEATURES = (
    "tc", "stock_z_dir", "market_z_dir", "rel_volume_5m", "move_from_open_atr_dir",
    "dist_vwap_atr_dir", "minutes_since_open", "spread_ratio", "risk_atr",
)


@dataclass
class MemoryItem:
    pad: str
    direction: int
    features: dict
    resolved_ts: float
    outcome_r: float
    exit_reason: str
    is_ghost: bool
    action: str


def vector(features: dict) -> np.ndarray:
    return np.array([float(features.get(k)) if features.get(k) is not None else np.nan for k in FEATURES])


class AttemptMemory:
    def __init__(self) -> None:
        self.items: list[MemoryItem] = []
        self._mat: np.ndarray | None = None

    def add(self, item: MemoryItem) -> None:
        self.items.append(item)
        self._mat = None

    def __len__(self) -> int:
        return len(self.items)

    def similar(self, pad: str, features: dict, now: float, k: int = 5) -> list[MemoryItem]:
        pool = [i for i, it in enumerate(self.items) if it.pad == pad and it.resolved_ts < now]
        if not pool:
            return []
        if self._mat is None:
            self._mat = np.vstack([vector(it.features) for it in self.items])
        m = self._mat[pool]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # all-missing feature columns
            mu = np.nanmean(m, axis=0)
            sd = np.nanstd(m, axis=0)
        sd[~np.isfinite(sd) | (sd == 0)] = 1.0
        mu[~np.isfinite(mu)] = 0.0
        z = (m - mu) / sd
        z[~np.isfinite(z)] = 0.0
        q = (vector(features) - mu) / sd
        q[~np.isfinite(q)] = 0.0
        d = np.sqrt(((z - q) ** 2).sum(axis=1))
        order = np.argsort(d, kind="stable")[:k]
        return [self.items[pool[i]] for i in order]


def describe(it: MemoryItem) -> str:
    f = it.features
    side = "LONG" if it.direction > 0 else "SHORT"
    tag = "ghost (not taken)" if it.is_ghost else "real trade"
    return (
        f"{side} {it.pad}, true count {f.get('tc', 0):+.1f}, market flow {f.get('market_z_dir', 0) or 0:+.1f}, "
        f"{(f.get('minutes_since_open') or 0) * 390:.0f} min after open -> {it.exit_reason} ({it.outcome_r:+.2f}R, {tag})"
    )
