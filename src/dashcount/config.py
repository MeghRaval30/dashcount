"""Settings: one YAML file -> typed config + a version tag.

The version tag (config hash + git commit) is stamped on every flight-recorder
row so different bot versions can be compared fairly.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


class AccountCfg(BaseModel):
    capital: float | None = None


class UniverseCfg(BaseModel):
    min_price: float
    min_avg_dollar_volume: float
    watchlist_size: int
    market_symbols: list[str]


class LevelMapCfg(BaseModel):
    rank_time: str
    data_cutoff: str
    earnings_window_days: int
    earnings_unknown_gap_pct: float
    observe_only_minutes: int
    last_entry: str
    flatten_at: str
    half_day_last_entry: str
    half_day_flatten_at: str
    release_block_before_min: int
    release_block_after_min: int
    fomc_block: list[str]


class FeaturesCfg(BaseModel):
    count_window_s: int
    count_sample_every_s: int
    count_halflife_min: float
    count_prior_kappa: float
    quote_fresh_s: float
    spread_median_window_min: int
    atr_1m_period: int
    atr_daily_period: int
    volume_profile_days: int
    feed_share_days: int


class OrbCfg(BaseModel):
    enabled: bool
    range_minutes: int
    window: list[str]
    volume_mult: float
    max_risk_atr_frac: float
    min_risk_pct: float


class VwapCfg(BaseModel):
    enabled: bool
    window: list[str]
    trend_lookback: int
    trend_frac: float
    high_within_min: int
    touch_atr: float
    stop_atr: float
    cooldown_min: int


class PadsCfg(BaseModel):
    orb: OrbCfg
    vwap_pullback: VwapCfg
    target_r: float


class HazardsCfg(BaseModel):
    spread_mult: float
    spread_max_pct: float
    dead_volume_mult: float
    already_ran_atr: float
    cost_mult: float
    stale_s_sip: float
    stale_s_iex: float
    death_cooldown_min: float


class GatesCfg(BaseModel):
    min_true_count: float
    market_against: float
    stock_weight: float
    market_weight: float


class SizingModeCfg(BaseModel):
    risk_pct: dict[str, float]
    max_notional_x: float
    max_open_risk_pct: float


class SizingCfg(BaseModel):
    tournament: SizingModeCfg
    real: SizingModeCfg
    liquidity_frac_of_5min: float
    short_buying_power_markup: float


class KronosCfg(BaseModel):
    enabled: bool
    model: str
    tokenizer: str
    max_context: int
    context_bars: int
    horizon_min: int
    samples: int
    min_odds: float
    min_decided: int
    timeout_s: float
    temperature: float
    top_p: float
    judge_from: str


class FinR1Cfg(BaseModel):
    enabled: bool
    model: str
    ollama_url: str
    timeout_s: float
    max_tokens: int
    memory_k: int


class VetoCfg(BaseModel):
    kronos: KronosCfg
    finr1: FinR1Cfg
    missed_jump_r: float
    coward_veto_rate: float
    min_vetoes_to_judge: int


class ExecutionCfg(BaseModel):
    entry_limit_r: float
    entry_timeout_s: float
    flip_true_count: float
    flip_hold_s: float
    stale_after_min: float
    stale_min_mfe_r: float
    checkpoint_r: float


class GuardianModeCfg(BaseModel):
    daily_loss_pct: float
    losers_in_row: int
    drawdown_pct: float


class GuardianCfg(BaseModel):
    tournament: GuardianModeCfg
    real: GuardianModeCfg
    heartbeat_timeout_s: float
    unknown_position_grace_s: float
    poll_s: float


class SimulatorCfg(BaseModel):
    latency_ms: float
    base_slippage_bps: float
    impact_bps: float
    max_slippage_bps: float
    sec_fee_per_million: float
    taf_per_share: float
    taf_cap: float
    starting_equity: float


class PracticeCfg(BaseModel):
    train: list[str]
    test: list[str]


class Config(BaseModel):
    mode: Literal["tournament", "real"]
    account: AccountCfg = AccountCfg()
    universe: UniverseCfg
    level_map: LevelMapCfg
    features: FeaturesCfg
    pads: PadsCfg
    hazards: HazardsCfg
    gates: GatesCfg
    sizing: SizingCfg
    veto: VetoCfg
    execution: ExecutionCfg
    guardian: GuardianCfg
    simulator: SimulatorCfg
    practice: PracticeCfg
    extra: dict = Field(default_factory=dict)

    # ---- mode helpers -------------------------------------------------
    @property
    def sizing_mode(self) -> SizingModeCfg:
        return self.sizing.tournament if self.mode == "tournament" else self.sizing.real

    @property
    def guardian_mode(self) -> GuardianModeCfg:
        return self.guardian.tournament if self.mode == "tournament" else self.guardian.real

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:10]

    def version(self) -> str:
        return f"{self.config_hash()}-{git_commit()}"


def _deep_update(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_update(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    """Load default.yaml, then an optional override file, then a dict of overrides."""
    data = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    if path and Path(path).resolve() != DEFAULT_CONFIG.resolve():
        data = _deep_update(data, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    if overrides:
        data = _deep_update(data, overrides)
    return Config.model_validate(data)


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5
        )
        commit = out.stdout.strip() or "nogit"
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "config"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return commit + ("+dirty" if dirty else "")
    except Exception:
        return "nogit"


def data_dir() -> Path:
    return Path(os.environ.get("DASHCOUNT_DATA", ROOT / "data"))
