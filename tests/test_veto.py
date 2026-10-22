import json
import time

import httpx
import numpy as np

from dashcount.models import LONG, SHORT, Candidate, VetoResult
from dashcount.veto import VetoCache, VetoChain
from dashcount.veto.finr1 import FinR1Veto
from dashcount.veto.kronos_veto import path_outcomes
from dashcount.veto.memory import AttemptMemory, MemoryItem
from dashcount.veto.packet import build, path_stats

from helpers import cfg


def _path(closes):
    c = np.array(closes, dtype=float)
    return np.stack([c, c + 0.5, c - 0.5, c], axis=1)


def test_path_outcomes_first_touch_long_and_short():
    up = _path([100, 101, 102, 103])
    down = _path([100, 99.5, 98.9, 98])
    flat = _path([100, 100.1, 99.9, 100])
    assert path_outcomes([up, down, flat], LONG, stop=99.0, target=102.0) == (1, 1)
    assert path_outcomes([up, down, flat], SHORT, stop=101.0, target=98.5) == (1, 1)
    # with the predicted range, the flat path's wiggle can touch both -> conservative loss
    assert path_outcomes([flat], LONG, stop=99.5, target=100.5, use_range=True) == (0, 1)


class FakeKronos:
    def __init__(self, verdict, odds=0.6, delay=0.0):
        self.verdict, self.odds, self.delay, self.calls = verdict, odds, delay, 0

    def assess(self, c, bars):
        self.calls += 1
        time.sleep(self.delay)
        return VetoResult("kronos", self.verdict, odds=self.odds, latency_s=self.delay)


class FakeFin:
    def __init__(self, verdict):
        self.verdict, self.calls, self.seen = verdict, 0, None

    def assess(self, text):
        self.calls += 1
        self.seen = text
        return VetoResult("finr1", self.verdict, "against_market" if self.verdict == "veto" else "none", latency_s=0.1)


def _cand():
    return Candidate("AAPL", 1.0, "orb", LONG, 200.0, 198.0, 204.0)


def test_chain_order_and_short_circuit(tmp_path):
    c = cfg()
    k, f = FakeKronos("veto"), FakeFin("allow")
    res = VetoChain(c.veto, k, f, tmp_path).run(_cand(), [], lambda o: "brief")
    assert res.final == "veto" and res.vetoed_by == "kronos" and f.calls == 0
    k2, f2 = FakeKronos("allow", odds=0.61), FakeFin("veto")
    res = VetoChain(c.veto, k2, f2, tmp_path / "b").run(_cand(), [], lambda o: f"odds={o}")
    assert res.final == "veto" and res.vetoed_by == "finr1" and f2.seen == "odds=0.61"
    res = VetoChain(c.veto, FakeKronos("allow"), FakeFin("allow"), tmp_path / "c").run(_cand(), [], lambda o: "x")
    assert res.final == "allow" and len(res.results) == 2


def test_chain_timeout_means_skip(tmp_path):
    c = cfg(veto={"kronos": {"timeout_s": 0.2}})
    res = VetoChain(c.veto, FakeKronos("allow", delay=3.0), FakeFin("allow"), tmp_path).run(_cand(), [], lambda o: "x")
    assert res.final == "unavailable"


def test_cache_reuses_answers(tmp_path):
    c = cfg()
    k = FakeKronos("allow")
    ch = VetoChain(c.veto, k, None, tmp_path)
    ch.run(_cand(), [], lambda o: "x")
    ch2 = VetoChain(c.veto, k, None, tmp_path)          # new chain, same cache dir
    res = ch2.run(_cand(), [], lambda o: "x")
    assert k.calls == 1 and res.results[0].cached
    cache = VetoCache(tmp_path / "kronos.jsonl")
    cache.put("t", VetoResult("kronos", "unavailable", "timeout"))
    assert cache.get("t") is None                       # failures are never cached


def _brief(**kw):
    b = dict(direction=1, pad="orb", entry=187.42, stop=186.10, target=190.06, sector="Information Technology",
             tc=2.4, stock_z_dir=2.9, market_z_dir=1.4, day_open=185.0, high=187.6, low=184.7, vwap=186.2,
             move_from_open_atr=0.6, rel_volume_5m=2.1, spread_ratio=1.1, last_closes=[185.0, 186.0, 187.42],
             spy_change=0.4, qqq_change=0.6, minutes_since_open=47, minutes_to_close=343, flags=["Friday"],
             open_positions=[], today_r=0.0, losers_today=0, kronos_odds=0.55,
             memory=[{"text": "LONG orb -> target (+2.00R)", "r": 2.0}])
    b.update(kw)
    return build(b)


def test_briefing_is_anonymised_and_rescaled():
    text = _brief()
    assert "187.42" not in text and "AAPL" not in text and "2025" not in text
    assert "Entry 100.00" in text
    assert "1 won, 0 lost" in text and "55%" in text


def test_path_stats():
    st = path_stats([100, 101, 100.5, 101.5, 102], 1)
    assert st["with_steps"] == 3 and st["changes"] == 2 and st["net_with"] == "with"


def test_memory_point_in_time_and_similarity():
    m = AttemptMemory()
    m.add(MemoryItem("orb", 1, {"tc": 3.0, "market_z_dir": 1.0}, 100.0, 2.0, "target", True, "x"))
    m.add(MemoryItem("orb", 1, {"tc": 1.0, "market_z_dir": -1.0}, 100.0, -1.0, "stop", False, "taken"))
    m.add(MemoryItem("orb", 1, {"tc": 3.1, "market_z_dir": 1.1}, 500.0, 2.0, "target", True, "x"))
    m.add(MemoryItem("vwap_pullback", 1, {"tc": 3.0}, 100.0, 2.0, "target", True, "x"))
    got = m.similar("orb", {"tc": 2.9, "market_z_dir": 0.9}, now=200.0, k=1)
    assert len(got) == 1 and got[0].resolved_ts == 100.0 and got[0].outcome_r == 2.0
    assert len(m.similar("orb", {"tc": 2.9}, now=200.0, k=5)) == 2


def _fin(handler):
    c = cfg()
    client = httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return FinR1Veto(c.veto.finr1, client)


def test_finr1_parses_constrained_json():
    def handler(req):
        body = json.loads(req.content)
        assert body["format"]["required"] and body["options"]["temperature"] == 0
        content = json.dumps({"reasoning": "checked", "verdict": "veto", "reason_code": "against_market",
                              "confidence": "high", "explanation": "market falling"})
        return httpx.Response(200, json={"message": {"content": content}})

    r = _fin(handler).assess("brief")
    assert r.verdict == "veto" and r.reason_code == "against_market" and r.details["reasoning"] == "checked"


def test_finr1_bad_output_or_error_is_unavailable():
    r = _fin(lambda req: httpx.Response(200, json={"message": {"content": "not json"}})).assess("b")
    assert r.verdict == "unavailable"
    r = _fin(lambda req: httpx.Response(500, text="boom")).assess("b")
    assert r.verdict == "unavailable"
    allow = json.dumps({"reasoning": "", "verdict": "allow", "reason_code": "other", "confidence": "low", "explanation": ""})
    r = _fin(lambda req: httpx.Response(200, json={"message": {"content": allow}})).assess("b")
    assert r.verdict == "allow" and r.reason_code == "none"
