"""Friend 2: Fin-R1, "the reasoning friend", running locally through Ollama.

Gets the anonymised briefing and must answer in a fixed JSON format. It can
only allow or veto; anything else (timeout, error, bad JSON) means skip.
"""

from __future__ import annotations

import json
import time

import httpx

from ..models import VetoResult

REASON_CODES = [
    "none", "against_market", "move_exhausted", "conflicting_signals", "choppy_price_action",
    "too_much_similar_exposure", "similar_setups_died", "other",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "verdict": {"type": "string", "enum": ["allow", "veto"]},
        "reason_code": {"type": "string", "enum": REASON_CODES},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "explanation": {"type": "string"},
    },
    "required": ["reasoning", "verdict", "reason_code", "confidence", "explanation"],
}

SYSTEM = """You are the risk-check partner of a fully automated intraday stock-trading bot.
The bot has ALREADY decided to take the trade described below: it passed every hazard check and its order-flow count is in favour.
Your only power is to VETO the trade or ALLOW it. You cannot change size, stop or target.

The DEFAULT ANSWER IS ALLOW. Most trades you see should be allowed: vetoing a good trade costs exactly as much as taking a bad one.
Veto ONLY if at least one of these red flags is clearly present in the data (check each one against the numbers):
- against_market: market flow is -1.0 or worse for this trade, OR both index funds moved more than 1% against the trade today.
- move_exhausted: the move from the open is more than 2.0x the usual daily range in the trade's direction AND the entry chases it.
- choppy_price_action: in the LAST 30 MINUTES SUMMARY the direction changed 18 or more times AND the net move is against the trade.
- similar_setups_died: MEMORY has at least 4 attempts and at least 75% of them lost.
- too_much_similar_exposure: the bot already holds 2 or more open positions in the same direction and sector.
- conflicting_signals: the true count is below +1.3 AND the pattern model gives under 45%.
- other: only for a different, equally concrete and serious problem.
If none of these is clearly present, ALLOW with reason_code "none". A small loss in memory, mild market weakness or ordinary wiggles are NOT reasons to veto.
Keep "reasoning" to at most 4 short sentences that name the numbers you checked, and "explanation" to one sentence.
Answer only with the JSON object."""


class FinR1Veto:
    name = "finr1"

    def __init__(self, cfg, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.client = client or httpx.Client(base_url=cfg.ollama_url, timeout=cfg.timeout_s + 5)

    def available(self) -> bool:
        try:
            r = self.client.get("/api/tags", timeout=3)
            names = {m["name"] for m in r.json().get("models", [])}
            return self.cfg.model in names or any(n.startswith(self.cfg.model) for n in names)
        except Exception:
            return False

    def warm_up(self) -> None:
        """Load the model into memory so the first real call isn't slow."""
        try:
            self.client.post("/api/generate", json={"model": self.cfg.model, "prompt": "", "keep_alive": "60m"}, timeout=120)
        except Exception:
            pass

    def assess(self, briefing: str) -> VetoResult:
        t0 = time.perf_counter()
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": briefing}],
            "format": SCHEMA,
            "stream": False,
            "keep_alive": "60m",
            "options": {"temperature": 0, "seed": 7, "num_predict": self.cfg.max_tokens, "num_ctx": 4096},
        }
        try:
            r = self.client.post("/api/chat", json=body, timeout=self.cfg.timeout_s)
            r.raise_for_status()
            content = r.json()["message"]["content"]
            ans = json.loads(content)
            verdict = ans.get("verdict")
            if verdict not in ("allow", "veto"):
                raise ValueError(f"bad verdict {verdict!r}")
            code = ans.get("reason_code") if ans.get("reason_code") in REASON_CODES else "other"
            if verdict == "allow":
                code = "none"
            return VetoResult(
                self.name, verdict, code, str(ans.get("confidence", "")), str(ans.get("explanation", ""))[:300],
                time.perf_counter() - t0, details={"reasoning": str(ans.get("reasoning", ""))[:1200]},
            )
        except httpx.TimeoutException:
            return VetoResult(self.name, "unavailable", "timeout", latency_s=time.perf_counter() - t0)
        except Exception as e:
            return VetoResult(self.name, "unavailable", "error", explanation=str(e)[:200],
                              latency_s=time.perf_counter() - t0)
