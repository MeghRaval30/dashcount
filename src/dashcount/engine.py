"""The engine: one trading day, event by event. Identical live and in practice.

Market data -> features -> jump pads -> hazards -> count gates -> bet size ->
veto chain -> executor, with every attempt (taken or not) recorded, and every
non-taken attempt followed as a ghost so the dataset has outcomes for skips too.
"""

from __future__ import annotations

import concurrent.futures as cf
import heapq
import itertools
import logging
import math
import queue
from dataclasses import dataclass
from typing import Callable

from . import exits, hazards, sizing
from .broker.sim import SimBroker
from .config import Config
from .executor import Executor, LiveTrade
from .features import SymbolState
from .guardian import GuardianRules
from .levelmap import DayPlan
from .models import Candidate, Quote, Trade, side_name
from .pads import evaluate_pads
from .recorder import Recorder
from .timeutil import fmt_et
from .veto import ChainResult, VetoChain
from .veto import packet as briefing
from .veto.memory import AttemptMemory, MemoryItem, describe

log = logging.getLogger(__name__)


@dataclass
class Ghost:
    cand: Candidate
    action: str
    vetoed_by: str
    track: exits.Track
    features: dict
    cost_r: float = 0.0        # estimated round-trip cost in R: a ghost never paid it


def friendly_action(action: str) -> str:
    """'skip_hazard:spread_blowout' -> 'skipped: spread blowout' (journal wording)."""
    kind, _, detail = action.partition(":")
    detail = detail.replace("_", " ")
    return {
        "skip_hazard": f"skipped: {detail} hazard",
        "skip_count": f"skipped: {detail}",
        "skip_size": "skipped: bet size came out zero",
        "skip_veto_unavailable": "skipped: AI check unavailable",
        "missed_jump": "skipped: price ran away (missed the jump)",
        "skip_late": f"skipped: {detail} by the time the AI answered",
    }.get(kind, action.replace("_", " "))


class VetoScore:
    """Ghost-trade scorecard per veto source (profit saved = -sum R of vetoed ghosts)."""

    def __init__(self, data: dict | None = None) -> None:
        self.d = data or {}

    def add_decision(self, source: str, verdict: str) -> None:
        s = self.d.setdefault(source, {"decisions": 0, "vetoes": 0, "resolved": 0, "saved_r": 0.0})
        s["decisions"] += 1
        if verdict == "veto":
            s["vetoes"] += 1

    def add_resolved_veto(self, source: str, ghost_r: float) -> None:
        s = self.d.setdefault(source, {"decisions": 0, "vetoes": 0, "resolved": 0, "saved_r": 0.0})
        s["resolved"] += 1
        s["saved_r"] += -ghost_r

    def switched_off(self, source: str, min_vetoes: int) -> bool:
        s = self.d.get(source)
        return bool(s and s["resolved"] >= min_vetoes and s["saved_r"] <= 0)

    def veto_rate(self, source: str) -> float | None:
        s = self.d.get(source)
        return s["vetoes"] / s["decisions"] if s and s["decisions"] >= 20 else None


class Engine:
    def __init__(
        self,
        cfg: Config,
        plan: DayPlan,
        broker,
        chain: VetoChain | None,
        recorder: Recorder,
        *,
        feed: str,
        live: bool = False,
        guardian: GuardianRules | None = None,
        guardian_block: Callable[[], str | None] | None = None,
        memory: AttemptMemory | None = None,
        veto_score: VetoScore | None = None,
        alert: Callable[[str], None] = lambda m: None,
        capital=None,
        journal: Callable[[dict], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.plan = plan
        self.session = plan.session
        self.broker = broker
        self.sim = broker if isinstance(broker, SimBroker) else None
        self.chain = chain
        self.rec = recorder
        self.feed = feed
        self.live = live
        self.guardian = guardian
        self.guardian_block_fn = guardian_block
        self.memory = memory if memory is not None else AttemptMemory()
        self.veto_score = veto_score or VetoScore()
        self.alert = alert
        self.capital = capital           # state.Capital (live budget) or None
        self.journal = journal           # live: structured events for the dashboard
        self.inbox: queue.Queue = queue.Queue()
        # live: veto calls run one at a time off the event thread
        self._outer = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="decide") if live else None

        self.states: dict[str, SymbolState] = {}
        for w in plan.watchlist:
            st = SymbolState(w, cfg.features, self.session.open, feed)
            st.bars.history = list(plan.history.get(w.symbol, []))
            self.states[w.symbol] = st
        self.market = [s for s in cfg.universe.market_symbols if s in self.states]

        self.now = self.session.open - 1
        self.next_minute = self.session.open + 60
        self._sched: list = []
        self._seq = itertools.count()
        self.executor = Executor(cfg.execution, broker, self._on_trade_closed, plan.flatten_at, alert)
        self.ghosts: dict[str, list[Ghost]] = {}
        self.attempt_features: dict[str, dict] = {}
        self.pending_veto: set[str] = set()
        self.veto_health: dict[str, dict] = {}   # per veto source: response times, errors, timeouts
        self.today_r = 0.0
        self.losers_today = 0
        self.counts = {"attempts": 0, "taken": 0}
        self.start_equity = self.account().equity
        if self.sim is not None:
            self.sim.start_day()
            self.sim.normal_volume_fn = self._sip_minute_volume
            self.sim.spread_bps_fn = (lambda s: self.states[s].nbbo_bps() if s in self.states else None) \
                if feed == "iex" else (lambda s: None)
        if self.guardian is not None:
            self.guardian.new_day(self.session.day.isoformat(), self.start_equity)
        self.finished = False

    # ------------------------------------------------------------ helpers
    def account(self):
        """The account as the bot sees it: its capital budget, not the whole account."""
        a = self.broker.account()
        return self.capital.view(a) if self.capital is not None else a

    def _sip_minute_volume(self, symbol: str, ts: float) -> float | None:
        st = self.states.get(symbol)
        return st.normal_minute_volume(ts, sip=True) if st else None

    def market_z(self) -> float:
        zs = [self.states[s].z(self.now) for s in self.market]
        return sum(zs) / len(zs) if zs else 0.0

    def tc_dir(self, symbol: str, direction: int) -> float:
        return sizing.true_count(self.states[symbol].z(self.now), self.market_z(), direction, self.cfg.gates)

    def schedule(self, due: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._sched, (due, next(self._seq), fn))

    def seed_today(self, bars: dict[str, list], upto: float) -> int:
        """Live start after the open: load today's earlier minutes so VWAP, the
        day's open/high/low, ATR and the setups see the whole session."""
        n = 0
        cutoff = float(int(upto // 60) * 60)
        for sym, lst in bars.items():
            st = self.states.get(sym)
            if st is None:
                continue
            done = [b for b in lst if self.session.open <= b.start and b.start + 60 <= cutoff]
            st.bars.seed(done)
            if done:
                st.last_price = done[-1].close
                n += len(done)
        self.next_minute = max(self.next_minute, cutoff + 60)
        self.now = max(self.now, cutoff)
        return n

    # -------------------------------------------------------------- time
    def advance_to(self, ts: float) -> None:
        while True:
            due = self._sched[0][0] if self._sched else math.inf
            nxt = min(self.next_minute, due)
            if nxt > ts:
                break
            self.now = max(self.now, nxt)
            if self.sim is not None:
                self.sim.advance(self.now)
            if due <= self.next_minute:
                _, _, fn = heapq.heappop(self._sched)
                fn()
            else:
                self._on_minute(self.next_minute)
                self.next_minute += 60
        self.now = max(self.now, ts)
        if self.sim is not None:
            self.sim.advance(self.now)
        self.executor.on_time(self.now)

    def on_quote(self, q: Quote) -> None:
        self.advance_to(q.ts)
        if self.sim is not None:
            self.sim.on_quote(q)
        st = self.states.get(q.symbol)
        if st is not None:
            st.on_quote(q)

    def on_trade(self, t: Trade) -> None:
        self.advance_to(t.ts)
        st = self.states.get(t.symbol)
        if st is None:
            return
        if self.sim is not None:
            self.sim.on_trade(t)
        st.on_trade(t)
        if self.executor.busy(t.symbol):
            tid = self.executor.by_symbol[t.symbol]
            tr = self.executor.trades[tid]
            had_cp = bool(tr.track and tr.track.checkpoint_done)
            self.executor.on_price(t.symbol, t.price, self.now, self.tc_dir(t.symbol, tr.direction))
            if not had_cp and tr.track is not None and tr.track.checkpoint_done:
                self._note("checkpoint", t.symbol, "+1R reached: stop moved to entry (checkpoint)")
        for g in list(self.ghosts.get(t.symbol, [])):
            self._update_ghost(g, t.price)

    def drain_inbox(self) -> None:
        """Live: apply veto results that finished on worker threads."""
        while True:
            try:
                fn = self.inbox.get_nowait()
            except queue.Empty:
                return
            fn()

    # ------------------------------------------------------------ minute
    def _on_minute(self, boundary: float) -> None:
        bars = {}
        for sym, st in self.states.items():
            b = st.on_minute(boundary)
            if b is not None:
                bars[sym] = b
        self._guardian_check()
        if boundary > self.plan.flatten_at:
            return
        if boundary >= self.plan.flatten_at:
            self._end_of_day_flatten()
            return
        for sym, b in bars.items():
            st = self.states[sym]
            if st.item.is_market:
                continue
            for c in evaluate_pads(st, b, self.session, self.cfg.pads):
                self._consider(c)

    def _guardian_check(self) -> None:
        if self.guardian is None:
            return
        acct = self.account()
        exposure = bool(self.broker.positions()) or bool(self.executor.active())
        for a in self.guardian.evaluate(self.now, acct.equity, self.plan.flatten_at, exposure):
            self._guardian_action(a.kind, a.reason)

    def _guardian_action(self, kind: str, reason: str) -> None:
        self.rec.add("guardian_events", {"ts": self.now, "kind": kind, "reason": reason})
        self._note("guardian", "", f"{kind}: {reason}")
        if kind == "flatten":
            self.executor.flatten_all(f"guardian:{reason}")
        elif kind == "alert":
            self.alert(reason)

    def guardian_block(self) -> str | None:
        if self.guardian is not None:
            return self.guardian.s.entry_block(self.session.day.isoformat())
        if self.guardian_block_fn is not None:
            return self.guardian_block_fn()
        return None

    def _end_of_day_flatten(self) -> None:
        self.executor.flatten_all("eod")

    # --------------------------------------------------------- decisions
    def _features(self, c: Candidate) -> tuple[dict, list[str], dict]:
        st = self.states[c.symbol]
        stock_z = st.z(self.now)
        mz = self.market_z()
        tc = sizing.true_count(stock_z, mz, c.direction, self.cfg.gates)
        ctx = hazards.HazardContext(
            now=self.now, feed=self.feed, blocked_reason=self.plan.blocked_reason(self.now),
            symbol_busy=self.executor.busy(c.symbol) or c.symbol in self.pending_veto,
            guardian_block=self.guardian_block(),
            slippage_bps=self.cfg.simulator.base_slippage_bps,
            sec_fee_per_million=self.cfg.simulator.sec_fee_per_million,
            taf_per_share=self.cfg.simulator.taf_per_share,
        )
        fired, vals = hazards.check(c, st, ctx, self.cfg.hazards)
        atr1 = st.bars.atr
        vwap = st.bars.vwap
        price = st.last_price or c.entry
        stock_z_alt = st.z_alt(self.now)
        f = {
            "tc": tc, "stock_z": stock_z, "market_z": mz,
            "stock_z_alt": stock_z_alt, "tc_alt": sizing.true_count(stock_z_alt, mz, c.direction, self.cfg.gates),
            "stock_z_dir": c.direction * stock_z, "market_z_dir": c.direction * mz,
            "imbalance": st.spread.imbalance() * c.direction,
            "rel_volume_5m": vals.get("rel_volume_5m"),
            "move_from_open_atr": vals.get("move_from_open_atr"),
            "move_from_open_atr_dir": (vals.get("move_from_open_atr") or 0.0) * c.direction,
            "dist_vwap_atr_dir": ((price - vwap) / atr1 * c.direction) if (vwap and atr1) else None,
            "minutes_since_open": (self.now - self.session.open) / 60 / 390,
            "spread_ratio": vals.get("spread_ratio"),
            "risk_atr": c.risk / st.item.atr_daily if st.item.atr_daily else None,
            "spread": vals.get("spread"), "spread_est": vals.get("spread_est"),
            "spread_bps_est": vals.get("spread_bps_est"), "nbbo_bps": st.nbbo_bps(),
            "target_to_cost": vals.get("target_to_cost"),
            "seconds_since_trade": vals.get("seconds_since_trade"),
            "atr_1m": atr1, "vwap": vwap, "price": price,
            "gap_pct": st.item.gap_pct, "premarket_rel_volume": st.item.premarket_rel_volume,
            "score": st.item.score, "feed_share": st.item.feed_share,
        }
        gate = sizing.gate(tc, mz, c.direction, self.cfg.gates)
        return f, fired, {"gate": gate, **vals}

    def _consider(self, c: Candidate) -> None:
        self.counts["attempts"] += 1
        f, fired, info = self._features(c)
        base = {
            "attempt_id": c.attempt_id, "day": self.session.day, "ts": c.ts, "time_et": fmt_et(c.ts),
            "symbol": c.symbol, "sector": self.states[c.symbol].item.sector, "pad": c.pad,
            "direction": c.direction, "entry": c.entry, "stop": c.stop, "target": c.target, "risk": c.risk,
            "hazards": fired, "gate": info.get("gate"), "blocked_reason": info.get("blocked_reason"),
            "guardian_block": info.get("guardian_block"), "flags": self.plan.flags, "feed": self.feed,
            **f,
        }
        if fired:
            return self._skip(c, base, "skip_hazard:" + fired[0], f)
        if info.get("gate"):
            return self._skip(c, base, "skip_count:" + info["gate"], f)
        acct = self.account()
        st = self.states[c.symbol]
        sip5 = (st.normal_minute_volume(self.now, sip=True) or 0) * 5 or None
        size = sizing.size_trade(c, f["tc"], acct.equity, acct.buying_power, self.executor.open_risk_dollars(),
                                 sip5, self.cfg.sizing_mode, self.cfg.sizing)
        base.update({"bucket": size.bucket, "risk_pct": size.risk_pct, "qty": size.qty,
                     "capped_by": size.capped_by, "equity": acct.equity})
        if size.qty <= 0:
            return self._skip(c, base, "skip_size:" + (size.skip_reason or ""), f)
        if self.chain is None:
            return self._after_veto(c, size.qty, base, f, ChainResult("allow"))

        # snapshot everything on this thread; the worker only adds Kronos's odds
        bars = self.states[c.symbol].bars.context(self.cfg.veto.kronos.context_bars)
        bdata = self._briefing_data(c, f)
        brief = lambda odds: briefing.build({**bdata, "kronos_odds": odds})  # noqa: E731
        self.pending_veto.add(c.symbol)
        if self.live:
            fut = self._outer.submit(self._run_chain, c, bars, brief)
            fut.add_done_callback(lambda fu: self.inbox.put(lambda: self._after_veto(c, size.qty, base, f, fu.result())))
        else:
            res = self._run_chain(c, bars, brief)
            self.schedule(self.now + res.latency_s, lambda: self._after_veto(c, size.qty, base, f, res))

    def _run_chain(self, c: Candidate, bars, brief) -> ChainResult:
        mv = self.cfg.veto.min_vetoes_to_judge
        k_on = self.cfg.veto.kronos.enabled and not self.veto_score.switched_off("kronos", mv)
        f_on = self.cfg.veto.finr1.enabled and not self.veto_score.switched_off("finr1", mv)
        saved = (self.chain.cfg.kronos.enabled, self.chain.cfg.finr1.enabled)
        try:
            self.chain.cfg.kronos.enabled, self.chain.cfg.finr1.enabled = k_on, f_on
            return self.chain.run(c, bars, brief)
        except Exception as e:
            log.exception("veto chain crashed")
            return ChainResult("unavailable", vetoed_by=f"error:{e}")
        finally:
            self.chain.cfg.kronos.enabled, self.chain.cfg.finr1.enabled = saved

    def _after_veto(self, c: Candidate, qty: int, base: dict, f: dict, res: ChainResult) -> None:
        self.pending_veto.discard(c.symbol)
        for r in res.results:
            self.veto_score.add_decision(r.source, r.verdict)
            h = self.veto_health.setdefault(r.source, {"last_s": 0.0, "avg_s": 0.0, "calls": 0, "errors": 0,
                                                       "timeouts": 0, "last_ts": 0.0})
            h["calls"] += 1
            h["last_s"] = r.latency_s
            h["avg_s"] += (r.latency_s - h["avg_s"]) / h["calls"]
            h["last_ts"] = self.now
            if r.verdict == "unavailable":
                h["timeouts" if r.reason_code == "timeout" else "errors"] += 1
            self.rec.add("veto_calls", {
                "attempt_id": c.attempt_id, "ts": self.now, "source": r.source, "verdict": r.verdict,
                "reason_code": r.reason_code, "confidence": r.confidence, "explanation": r.explanation,
                "latency_s": r.latency_s, "odds": r.odds, "cached": r.cached, "details": r.details,
            })
            rate = self.veto_score.veto_rate(r.source)
            if rate is not None and rate > self.cfg.veto.coward_veto_rate:
                base.setdefault("veto_warnings", []).append(f"{r.source} veto rate {rate:.0%}")
        k = res.get("kronos")
        fr = res.get("finr1")
        base.update({
            "kronos_verdict": k.verdict if k else None, "kronos_odds": k.odds if k else None,
            "finr1_verdict": fr.verdict if fr else None, "finr1_reason": fr.reason_code if fr else None,
            "veto_latency_s": res.latency_s, "decided_ts": self.now,
        })
        f = dict(f, kronos_odds=k.odds if k else None)
        if res.final == "veto":
            return self._skip(c, base, f"vetoed:{res.vetoed_by}", f, vetoed_by=res.vetoed_by)
        if res.final != "allow":
            return self._skip(c, base, "skip_veto_unavailable", f)
        st = self.states[c.symbol]
        price = st.last_price or c.entry
        if (price - c.entry) * c.direction > self.cfg.veto.missed_jump_r * c.risk:
            return self._skip(c, base, "missed_jump", f)
        block = self.guardian_block() or self.plan.blocked_reason(self.now)
        if self.executor.busy(c.symbol) or block:
            return self._skip(c, base, "skip_late:" + (block or "busy"), f)
        self.counts["taken"] += 1
        log.info("TAKEN %s %s %s qty %d entry %.2f stop %.2f target %.2f (true count %+.2f, bet %s%%)",
                 c.symbol, c.pad, side_name(c.direction), qty, c.entry, c.stop, c.target, f.get("tc") or 0.0,
                 base.get("risk_pct"))
        base["action"] = "taken"
        self.rec.add("attempts", base)
        self.attempt_features[c.attempt_id] = f
        t = self.executor.enter(c, qty, self.now, meta={"attempt": base})
        # the plan travels with the order id, so the dashboard can measure slippage at the broker
        self._note("taken", c.symbol, f"{c.pad.replace('_', ' ')} {side_name(c.direction).lower()} -> TAKEN "
                   f"{qty} sh, bet {base.get('risk_pct')}%, stop {c.stop:.2f} target {c.target:.2f}",
                   tc=round(f.get("tc") or 0.0, 2), action="taken", trade_id=t.trade_id, entry=c.entry,
                   stop=c.stop, target=c.target, qty=int(qty), direction=c.direction)

    def _note(self, kind: str, symbol: str, text: str, **extra) -> None:
        if self.journal is not None:
            try:
                self.journal({"ts": self.now, "kind": kind, "symbol": symbol, "text": text, **extra})
            except Exception:
                log.exception("journal write failed")

    def _skip(self, c: Candidate, base: dict, action: str, f: dict, vetoed_by: str = "") -> None:
        k = base.get("kronos_odds")
        fr = base.get("finr1_reason")
        why = friendly_action(action)
        if action.startswith("vetoed:kronos") and k is not None:
            why = f"vetoed by Kronos ({k * 100:.0f}% odds)"
        elif action.startswith("vetoed:finr1"):
            why = f"vetoed by Fin-R1 ({(fr or 'other').replace('_', ' ')})"
        self._note("veto" if action.startswith("vetoed") else "setup", c.symbol,
                   f"{c.pad.replace('_', ' ')} {side_name(c.direction).lower()} -> {why}",
                   tc=round(f.get("tc") or 0.0, 2), action=action)
        if self.live:
            log.info("setup %s %s %s -> %s (true count %+.2f)", c.symbol, c.pad, side_name(c.direction),
                     action, f.get("tc") or 0.0)
        base["action"] = action
        base.setdefault("decided_ts", self.now)
        self.rec.add("attempts", base)
        tr = exits.Track(c.direction, c.entry, c.stop, c.target, c.risk, opened_ts=self.now)
        ttc = f.get("target_to_cost")
        cost_r = abs(c.target - c.entry) / ttc / c.risk if (ttc and c.risk) else 0.0
        self.ghosts.setdefault(c.symbol, []).append(Ghost(c, action, vetoed_by, tr, f, cost_r))

    # ------------------------------------------------------------ ghosts
    def _update_ghost(self, g: Ghost, price: float) -> None:
        tr = g.track
        exits.update_excursions(tr, price)
        hit = exits.ghost_exit(tr, price)
        if hit:
            return self._resolve_ghost(g, *hit)
        if exits.checkpoint_due(tr, price, self.cfg.execution.checkpoint_r):
            exits.apply_checkpoint(tr)
        reason = exits.soft_exit(tr, self.now, self.tc_dir(g.cand.symbol, tr.direction), self.plan.flatten_at,
                                 self.cfg.execution)
        if reason:
            self._resolve_ghost(g, reason, price)

    def _resolve_ghost(self, g: Ghost, reason: str, exit_price: float) -> None:
        lst = self.ghosts.get(g.cand.symbol, [])
        if g in lst:
            lst.remove(g)
        r = g.track.r(exit_price)
        r_net = r - g.cost_r
        self.rec.add("outcomes", {
            "attempt_id": g.cand.attempt_id, "is_ghost": True, "action": g.action, "exit_reason": reason,
            "r": r, "r_net": r_net, "mfe_r": g.track.mfe_r, "mae_r": g.track.mae_r,
            "hold_s": self.now - g.track.opened_ts, "exit_price": exit_price, "resolved_ts": self.now,
        })
        # vetoes and memory are judged on what the setup would really have made
        if g.vetoed_by:
            self.veto_score.add_resolved_veto(g.vetoed_by, r_net)
        self.memory.add(MemoryItem(g.cand.pad, g.cand.direction, g.features, self.now, r_net, reason, True, g.action))

    # ------------------------------------------------------- real trades
    def _on_trade_closed(self, t: LiveTrade) -> None:
        f = self.attempt_features.pop(t.attempt_id, {})
        risk = abs(t.avg_entry - t.planned_stop) if t.avg_entry else abs(t.planned_entry - t.planned_stop)
        filled = t.qty_filled
        pnl = t.pnl() if filled else 0.0
        fees = self._fees(t) if filled else 0.0
        if self.sim is None:
            realistic = pnl - self._slippage_cost(t) - fees
        else:
            realistic = pnl - fees
        r = pnl / (risk * filled) if (risk and filled) else 0.0
        row = {
            "trade_id": t.trade_id, "attempt_id": t.attempt_id, "day": self.session.day, "symbol": t.symbol,
            "pad": t.pad, "direction": t.direction, "qty_req": t.qty_req, "qty_filled": filled,
            "planned_entry": t.planned_entry, "avg_entry": t.avg_entry, "stop": t.planned_stop, "target": t.target,
            "exit_avg": t.exit_avg, "exit_reason": t.exit_reason, "submitted_ts": t.submitted_ts,
            "filled_ts": t.filled_ts, "closed_ts": t.closed_ts, "pnl": pnl, "pnl_realistic": realistic,
            "fees": fees, "r": r, "mfe_r": t.track.mfe_r if t.track else None,
            "mae_r": t.track.mae_r if t.track else None,
            "checkpoint": bool(t.track and t.track.checkpoint_done),
        }
        self.rec.add("trades", row)
        if not filled:
            self.rec.add("outcomes", {"attempt_id": t.attempt_id, "is_ghost": False, "action": "taken",
                                      "exit_reason": t.exit_reason, "r": 0.0, "resolved_ts": self.now})
            return
        self.rec.add("outcomes", {
            "attempt_id": t.attempt_id, "is_ghost": False, "action": "taken", "exit_reason": t.exit_reason,
            "r": r, "mfe_r": row["mfe_r"], "mae_r": row["mae_r"], "hold_s": t.closed_ts - t.filled_ts,
            "exit_price": t.exit_avg, "resolved_ts": self.now, "pnl": pnl, "pnl_realistic": realistic,
        })
        self._note("exit", t.symbol, f"{side_name(t.direction).lower()} closed: {t.exit_reason.replace('_', ' ')} "
                   f"{pnl:+.2f} ({r:+.2f}R)", pnl=round(pnl, 2), r=round(r, 2), action=t.exit_reason)
        self.today_r += r
        if pnl < 0:
            self.losers_today += 1
            self.states[t.symbol].last_loss_exit_ts = self.now
        self.memory.add(MemoryItem(t.pad, t.direction, f, self.now, r, t.exit_reason, False, "taken"))
        if self.guardian is not None:
            for a in self.guardian.on_trade_closed(pnl):
                self._guardian_action(a.kind, a.reason)
        if self.live:
            self.alert(f"{'WIN' if pnl > 0 else 'LOSS'} {side_name(t.direction)} {t.symbol} "
                       f"{t.exit_reason}: {pnl:+.2f} ({r:+.2f}R)")

    def _fees(self, t: LiveTrade) -> float:
        sim = self.cfg.simulator
        sell_px = t.exit_avg if t.direction > 0 else t.avg_entry
        q = t.qty_filled
        return sell_px * q * sim.sec_fee_per_million / 1e6 + min(q * sim.taf_per_share, sim.taf_cap)

    def _slippage_cost(self, t: LiveTrade) -> float:
        sim = self.cfg.simulator
        nv = self._sip_minute_volume(t.symbol, t.filled_ts or self.now)
        bps = sim.base_slippage_bps + (sim.impact_bps * t.qty_filled / nv if nv else 0.0)
        bps = min(bps, sim.max_slippage_bps)
        return t.qty_filled * (t.avg_entry + t.exit_avg) * bps / 1e4

    # ---------------------------------------------------------- briefing
    def _briefing_data(self, c: Candidate, f: dict) -> dict:
        st = self.states[c.symbol]
        closes = [b.close for b in st.bars.bars[-30:]]

        def chg(sym):
            s = self.states.get(sym)
            if not s or not s.last_price or not s.item.prev_close:
                return None
            return (s.last_price / s.item.prev_close - 1) * 100

        mem = self.memory.similar(c.pad, f, self.now, self.cfg.veto.finr1.memory_k)
        b = {
            "direction": c.direction, "pad": c.pad, "entry": c.entry, "stop": c.stop, "target": c.target,
            "sector": st.item.sector, "tc": f["tc"], "stock_z_dir": f["stock_z_dir"],
            "market_z_dir": f["market_z_dir"], "imbalance": f["imbalance"],
            "day_open": st.bars.day_open, "high": st.bars.high if st.bars.high > -math.inf else None,
            "low": st.bars.low if st.bars.low < math.inf else None, "vwap": st.bars.vwap,
            "move_from_open_atr": f.get("move_from_open_atr"), "rel_volume_5m": f.get("rel_volume_5m"),
            "spread_ratio": f.get("spread_ratio"), "last_closes": closes,
            "spy_change": chg("SPY"), "qqq_change": chg("QQQ"),
            "minutes_since_open": (self.now - self.session.open) / 60,
            "minutes_to_close": (self.session.close - self.now) / 60, "flags": self.plan.flags,
            "open_positions": [(t.direction, self.states[t.symbol].item.sector) for t in self.executor.active()],
            "today_r": self.today_r, "losers_today": self.losers_today, "kronos_odds": None,
            "memory": [{"text": describe(m), "r": m.outcome_r} for m in mem],
        }
        return b

    # ---------------------------------------------------------- snapshot
    def snapshot(self, extra: dict | None = None) -> dict:
        """Everything the dashboard shows, as plain JSON-able data."""
        now = self.now
        mz = self.market_z()
        acct = self.account()

        def chg(st):
            p = st.last_price
            return (p / st.item.prev_close - 1) * 100 if (p and st.item.prev_close) else None

        syms = []
        for sym, st in self.states.items():
            z = st.z(now)
            sp, nsp = st.spread.spread, st.spread.normal()
            syms.append({
                "symbol": sym, "sector": st.item.sector, "market": st.item.is_market,
                "price": st.last_price, "change_pct": chg(st), "z": round(z, 3),
                "tc_long": round(sizing.true_count(z, mz, 1, self.cfg.gates), 3),
                "tc_short": round(sizing.true_count(z, mz, -1, self.cfg.gates), 3),
                "vwap": st.bars.vwap, "spread_ratio": (sp / nsp) if (sp and nsp) else None,
                "last_trade_age": (now - st.last_trade_ts) if st.last_trade_ts else None,
                "busy": self.executor.busy(sym), "score": st.item.score, "gap_pct": st.item.gap_pct,
            })
        trades = []
        for t in self.executor.active():
            st = self.states.get(t.symbol)
            price = st.last_price if st else None
            tr = t.track
            trades.append({
                "symbol": t.symbol, "direction": t.direction, "pad": t.pad, "state": t.state,
                "qty": t.qty_filled or t.qty_req, "entry": t.avg_entry or t.planned_entry,
                "stop": tr.stop if tr else t.planned_stop, "target": t.target, "price": price,
                "r": tr.r(price) if (tr and price) else 0.0, "mfe_r": tr.mfe_r if tr else 0.0,
                "checkpoint": bool(tr and tr.checkpoint_done), "opened_ts": t.filled_ts or t.submitted_ts,
                "risk_dollars": t.open_risk,
            })
        closed = [t for t in self.executor.closed if t.qty_filled]

        def idx_chg(sym):
            st = self.states.get(sym)
            return chg(st) if st else None

        return {
            "ts": now, "day": self.session.day.isoformat(), "mode": self.cfg.mode,
            "session": {"open": self.session.open, "close": self.session.close, "half_day": self.session.half_day,
                        "flatten_at": self.plan.flatten_at, "last_entry": self.plan.last_entry},
            "budget": {"capital": self.cfg.account.capital, "equity": acct.equity, "start_equity": self.start_equity,
                       "pnl": acct.equity - self.start_equity},
            "today_r": self.today_r, "losers_today": self.losers_today, "counts": dict(self.counts),
            "closed_today": [{"symbol": t.symbol, "direction": t.direction, "pnl": t.pnl(), "exit": t.exit_reason,
                              "closed_ts": t.closed_ts} for t in closed],
            "market": {"z": round(mz, 3), "spy_change": idx_chg("SPY"), "qqq_change": idx_chg("QQQ")},
            "symbols": syms, "trades": trades, "flags": self.plan.flags,
            "blocked_now": self.plan.blocked_reason(now), "guardian_block": self.guardian_block(),
            "veto_score": self.veto_score.d, "veto_health": self.veto_health, **(extra or {}),
        }

    def bars_snapshot(self) -> dict:
        """Today's 1-minute bars + VWAP per symbol, for the dashboard's price charts."""
        out = {}
        for sym, st in self.states.items():
            bs = st.bars.bars
            out[sym] = {
                "prev_close": st.item.prev_close,
                "bars": [[b.start, round(b.open, 4), round(b.high, 4), round(b.low, 4), round(b.close, 4), b.volume]
                         for b in bs],
                "vwap": [round(v, 4) for v in st.bars.vwaps],
            }
        return {"ts": self.now, "day": self.session.day.isoformat(), "open": self.session.open,
                "close": self.session.close, "symbols": out}

    # --------------------------------------------------------------- end
    def finish(self) -> dict:
        """Close the day: resolve remaining ghosts, write the daily row."""
        if self.finished:
            return {}
        self.advance_to(self.session.close)
        for sym, lst in list(self.ghosts.items()):
            for g in list(lst):
                st = self.states[sym]
                self._resolve_ghost(g, "eod", st.last_price or g.cand.entry)
        acct = self.account()
        trades = [t for t in self.executor.closed if t.qty_filled > 0]
        row = {
            "day": self.session.day, "start_equity": self.start_equity, "end_equity": acct.equity,
            "pnl": acct.equity - self.start_equity, "trades": len(trades),
            "wins": sum(1 for t in trades if t.pnl() > 0), "losses": sum(1 for t in trades if t.pnl() < 0),
            "attempts": self.counts["attempts"], "today_r": self.today_r,
            "watchlist": [w.symbol for w in self.plan.watchlist], "flags": self.plan.flags,
            "open_positions_at_end": len(self.broker.positions()),
        }
        self.rec.add("daily", row)
        self.finished = True
        return row
