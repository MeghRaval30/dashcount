"""Friend 1: Kronos, "the pattern eye".

Feeds the stock's recent 1-minute candles to the Kronos candlestick foundation
model, samples N possible next-30-minute paths, and measures how many hit the
target before the stop.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import ROOT
from ..models import Bar, Candidate, VetoResult
from ..timeutil import ET

log = logging.getLogger(__name__)

_lock = threading.Lock()
_loaded: dict = {}


def _import_kronos():
    path = str(ROOT / "vendor" / "Kronos")
    if path not in sys.path:
        sys.path.insert(0, path)
    from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore

    return Kronos, KronosTokenizer, KronosPredictor


def load_predictor(cfg, device: str | None = None):
    """Load (once) and return a KronosPredictor."""
    key = (cfg.model, cfg.tokenizer, device)
    with _lock:
        if key not in _loaded:
            import torch

            Kronos, KronosTokenizer, KronosPredictor = _import_kronos()
            dev = device or ("cuda:0" if torch.cuda.is_available() else "cpu")
            tok = KronosTokenizer.from_pretrained(cfg.tokenizer)
            model = Kronos.from_pretrained(cfg.model)
            tok.eval()
            model.eval()
            _loaded[key] = KronosPredictor(model, tok, device=dev, max_context=cfg.max_context)
            log.info("Kronos loaded on %s", dev)
        return _loaded[key]


def bars_frame(bars: list[Bar]) -> tuple[pd.DataFrame, pd.Series]:
    df = pd.DataFrame({
        "open": [b.open for b in bars],
        "high": [b.high for b in bars],
        "low": [b.low for b in bars],
        "close": [b.close for b in bars],
        "volume": [b.volume for b in bars],
    })
    df["amount"] = df["volume"] * (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ts = pd.Series(pd.to_datetime([b.start for b in bars], unit="s", utc=True)).dt.tz_convert(ET).dt.tz_localize(None)
    return df, ts


def path_outcomes(paths: list[np.ndarray], direction: int, stop: float, target: float,
                  use_range: bool = False) -> tuple[int, int]:
    """Count (wins, losses) over sampled paths. Each path is (horizon, 4) OHLC.

    By default only predicted closes are used: Kronos's predicted highs/lows are
    noisy (high < low happens), which would fake touches of both levels. With
    use_range=True a bar touching both counts as a loss (conservative)."""
    wins = losses = 0
    for p in paths:
        if use_range:
            hi = p[:, :4].max(axis=1)
            lo = p[:, :4].min(axis=1)
        else:
            hi = lo = p[:, 3]
        for h, l in zip(hi, lo):
            if direction > 0:
                hit_stop, hit_tgt = l <= stop, h >= target
            else:
                hit_stop, hit_tgt = h >= stop, l <= target
            if hit_stop:
                losses += 1
                break
            if hit_tgt:
                wins += 1
                break
    return wins, losses


class KronosVeto:
    name = "kronos"

    def __init__(self, cfg, device: str | None = None, predictor=None) -> None:
        self.cfg = cfg
        self.device = device
        self._predictor = predictor
        # Kronos keeps internal caches: never run two inferences at once
        self._run_lock = threading.Lock()

    @property
    def predictor(self):
        if self._predictor is None:
            self._predictor = load_predictor(self.cfg, self.device)
        return self._predictor

    def sample_paths(self, bars: list[Bar], seed: int) -> list[np.ndarray]:
        import torch

        df, ts = bars_frame(bars)
        last = ts.iloc[-1]
        y_ts = pd.Series(pd.date_range(last + pd.Timedelta(minutes=1), periods=self.cfg.horizon_min, freq="min"))
        n = self.cfg.samples
        predictor = self.predictor
        with self._run_lock:
            torch.manual_seed(seed)
            np.random.seed(seed % (2**32 - 1))
            preds = predictor.predict_batch(
                [df] * n, [ts] * n, [y_ts] * n, pred_len=self.cfg.horizon_min,
                T=self.cfg.temperature, top_p=self.cfg.top_p, sample_count=1, verbose=False,
            )
        return [p[["open", "high", "low", "close"]].to_numpy() for p in preds]

    def assess(self, c: Candidate, bars: list[Bar]) -> VetoResult:
        t0 = time.perf_counter()
        bars = [b for b in bars if b.volume > 0 or b.high != b.low][-self.cfg.context_bars:]
        if len(bars) < 60:
            return VetoResult(self.name, "allow", "insufficient_context", explanation=f"only {len(bars)} bars",
                              latency_s=time.perf_counter() - t0)
        seed = int(hashlib.sha256(f"{c.symbol}{c.ts}{c.direction}".encode()).hexdigest()[:8], 16)
        paths = self.sample_paths(bars, seed)
        wins, losses = path_outcomes(paths, c.direction, c.stop, c.target)
        decided = wins + losses
        odds = wins / decided if decided else None
        lat = time.perf_counter() - t0
        details = {"wins": wins, "losses": losses, "samples": len(paths),
                   "path_closes": [np.round(p[:, 3], 4).tolist() for p in paths]}
        if decided < self.cfg.min_decided:
            return VetoResult(self.name, "allow", "uncertain", "low", f"only {decided}/{len(paths)} paths decided",
                              lat, odds, details)
        if odds < self.cfg.min_odds:
            return VetoResult(self.name, "veto", "kronos_odds_low", "medium",
                              f"{wins}/{decided} paths hit target first", lat, odds, details)
        return VetoResult(self.name, "allow", "none", "medium", f"{wins}/{decided} paths hit target first",
                          lat, odds, details)
