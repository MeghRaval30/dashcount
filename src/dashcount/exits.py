"""Exit rules as pure functions, shared by real trades (executor) and ghosts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Track:
    direction: int
    entry: float
    stop: float
    target: float
    risk: float               # initial risk distance in price
    opened_ts: float
    mfe_r: float = 0.0        # max favourable excursion, in R
    mae_r: float = 0.0        # max adverse excursion, in R (<= 0)
    checkpoint_done: bool = False
    flip_since: float | None = None

    def r(self, price: float) -> float:
        return (price - self.entry) * self.direction / self.risk if self.risk > 0 else 0.0


def update_excursions(t: Track, price: float) -> None:
    r = t.r(price)
    if r > t.mfe_r:
        t.mfe_r = r
    if r < t.mae_r:
        t.mae_r = r


def touched(t: Track, price: float) -> str | None:
    """Did this print touch the stop or the target? (ghosts / simulator)."""
    if (price - t.stop) * t.direction <= 0:
        return "stop"
    if (price - t.target) * t.direction >= 0:
        return "target"
    return None


def checkpoint_due(t: Track, price: float, checkpoint_r: float) -> bool:
    return (not t.checkpoint_done) and t.r(price) >= checkpoint_r


def apply_checkpoint(t: Track) -> None:
    """Move the stop to the entry: from here the trade can't lose."""
    t.stop = t.entry
    t.checkpoint_done = True


def soft_exit(t: Track, now: float, tc_dir: float | None, flatten_at: float, cfg) -> str | None:
    """Exits our own code runs (the broker holds stop and target)."""
    if now >= flatten_at:
        return "eod"
    if tc_dir is not None and tc_dir <= cfg.flip_true_count:
        if t.flip_since is None:
            t.flip_since = now
        elif now - t.flip_since >= cfg.flip_hold_s:
            return "count_flip"
    else:
        t.flip_since = None
    if now - t.opened_ts >= cfg.stale_after_min * 60 and t.mfe_r < cfg.stale_min_mfe_r:
        return "stale"
    return None
