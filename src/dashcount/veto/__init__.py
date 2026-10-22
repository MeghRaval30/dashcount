"""The veto chain: Kronos first (fast), then Fin-R1 (only if Kronos allows).

Veto-only: the chain can block an entry, never create or change one.
Timeouts, errors and bad answers all mean "skip" (verdict "unavailable").
Answers are cached by input, so practice re-runs don't recompute them.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..models import Bar, Candidate, VetoResult

log = logging.getLogger(__name__)


class VetoCache:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    self._d[rec["key"]] = rec["result"]
                except Exception:
                    continue

    def get(self, key: str) -> VetoResult | None:
        r = self._d.get(key)
        if r is None:
            return None
        v = VetoResult(**r)
        v.cached = True
        return v

    def put(self, key: str, v: VetoResult) -> None:
        if v.verdict == "unavailable":
            return                        # don't cache timeouts/errors: they depend on the machine
        d = asdict(v)
        d["cached"] = False
        with self._lock:
            self._d[key] = d
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "result": d}) + "\n")


@dataclass
class ChainResult:
    final: str                                 # allow | veto | unavailable
    results: list[VetoResult] = field(default_factory=list)
    vetoed_by: str = ""

    @property
    def latency_s(self) -> float:
        return sum(r.latency_s for r in self.results)

    def get(self, source: str) -> VetoResult | None:
        return next((r for r in self.results if r.source == source), None)


def _key(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


class VetoChain:
    def __init__(self, veto_cfg, kronos=None, finr1=None, cache_dir: Path | None = None) -> None:
        self.cfg = veto_cfg
        self.kronos = kronos
        self.finr1 = finr1
        self.kcache = VetoCache(cache_dir / "kronos.jsonl" if cache_dir else None)
        self.fcache = VetoCache(cache_dir / "finr1.jsonl" if cache_dir else None)
        self._pool = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="veto")

    def _timed(self, fn: Callable[[], VetoResult], source: str, timeout: float) -> VetoResult:
        fut = self._pool.submit(fn)
        try:
            v = fut.result(timeout=timeout + 2.0)
        except cf.TimeoutError:
            return VetoResult(source, "unavailable", "timeout", latency_s=timeout)
        except Exception as e:  # model crashed
            log.exception("%s veto failed", source)
            return VetoResult(source, "unavailable", "error", explanation=str(e)[:200])
        if v.verdict != "unavailable" and v.latency_s > timeout:
            v = VetoResult(source, "unavailable", "timeout", latency_s=v.latency_s, details={"late": asdict(v)})
        return v

    def run(self, c: Candidate, bars: list[Bar], briefing_fn: Callable[[float | None], str]) -> ChainResult:
        out = ChainResult("allow")
        odds = None
        if self.kronos is not None and self.cfg.kronos.enabled:
            kc = self.cfg.kronos
            key = _key("kronos", kc.model, kc.samples, kc.horizon_min, kc.min_odds, c.symbol, c.ts, c.direction,
                       round(c.entry, 4), round(c.stop, 4), round(c.target, 4))
            v = self.kcache.get(key)
            if v is None:
                v = self._timed(lambda: self.kronos.assess(c, bars), "kronos", kc.timeout_s)
                self.kcache.put(key, v)
            out.results.append(v)
            odds = v.odds
            if v.verdict != "allow":
                out.final = v.verdict
                out.vetoed_by = "kronos" if v.verdict == "veto" else ""
                return out
        if self.finr1 is not None and self.cfg.finr1.enabled:
            fc = self.cfg.finr1
            text = briefing_fn(odds)
            from .packet import digest

            key = digest(text, fc.model)
            v = self.fcache.get(key)
            if v is None:
                v = self._timed(lambda: self.finr1.assess(text), "finr1", fc.timeout_s)
                self.fcache.put(key, v)
            v.details = {**v.details, "briefing": text}
            out.results.append(v)
            if v.verdict != "allow":
                out.final = v.verdict
                out.vetoed_by = "finr1" if v.verdict == "veto" else ""
        return out
