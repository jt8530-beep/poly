"""
Config for Overround Arb scanner. Env-driven, stdlib only.

Same _clean() trick as wallet_alpha_radar/alpha_radar/config.py — strips
inline `#` comments so a `KEY=value   # comment` line works regardless of
whether the .env is sourced via systemd, docker, dotenv, etc.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path


def _clean(v: str | None) -> str:
    if v is None:
        return ""
    s = v
    cut = -1
    for i, ch in enumerate(s):
        if ch == "#" and (i == 0 or s[i - 1].isspace()):
            cut = i
            break
    if cut >= 0:
        s = s[:cut]
    return s.strip()


def _env_f(name, default):
    v = _clean(os.getenv(name))
    return float(v) if v else default

def _env_i(name, default):
    v = _clean(os.getenv(name))
    return int(v) if v else default

def _env_s(name, default=""):
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = _clean(raw)
    return cleaned if cleaned else default

def _env_b(name, default):
    v = _clean(os.getenv(name))
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")


@dataclass
class ApiConfig:
    gamma_url:    str   = _env_s("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob_url:     str   = _env_s("POLY_CLOB_URL",  "https://clob.polymarket.com")
    timeout_sec:  float = _env_f("OA_HTTP_TIMEOUT", 15.0)
    sleep_between: float = _env_f("OA_HTTP_SLEEP", 0.20)
    max_retries:  int   = _env_i("OA_HTTP_RETRIES", 4)
    user_agent:   str   = _env_s("OA_USER_AGENT", "overround-arb/0.1")


@dataclass
class ScanConfig:
    # event-level filters
    min_outcomes:        int   = _env_i("OA_MIN_OUTCOMES", 5)        # at least this many active markets
    min_event_volume:    float = _env_f("OA_MIN_VOLUME", 50_000.0)   # event lifetime $ volume
    days_until_end_min:  int   = _env_i("OA_DAYS_END_MIN", 1)        # at least this many days of life left
    days_until_end_max:  int   = _env_i("OA_DAYS_END_MAX", 730)      # but not infinite-horizon

    # market-level filters (applied to markets within each event)
    max_market_ask:      float = _env_f("OA_MAX_MARKET_ASK", 0.99)   # exclude eliminated/no-liquidity placeholders
    min_market_ask:      float = _env_f("OA_MIN_MARKET_ASK", 0.0)    # > this; 0.0 means any positive ask

    # opportunity classification thresholds
    arb_max:             float = _env_f("OA_ARB_MAX", 1.00)          # total_ask < this  → "arb"
    near_arb_max:        float = _env_f("OA_NEAR_ARB_MAX", 1.05)     # < this           → "near_arb"
    premium_max:         float = _env_f("OA_PREMIUM_MAX", 1.20)      # < this           → "premium_harvest"
    premium_min_days:    int   = _env_i("OA_PREMIUM_MIN_DAYS", 30)   # premium_harvest needs this much horizon

    # sanity range — anything outside this is flagged as "anomalous"
    sanity_total_min:    float = _env_f("OA_SANITY_MIN", 0.50)
    sanity_total_max:    float = _env_f("OA_SANITY_MAX", 1.50)

    # paging
    events_page_size:    int   = _env_i("OA_EVENTS_PAGE", 100)
    events_max_pages:    int   = _env_i("OA_EVENTS_MAX_PAGES", 20)


@dataclass
class PaperTradeConfig:
    # Position sizing
    budget_per_basket:    float = _env_f("OA_PT_BUDGET", 100.0)
    max_open_positions:   int   = _env_i("OA_PT_MAX_POSITIONS", 50)

    # Entry filter (which scanner classifications to act on, comma-separated)
    entry_classes:        tuple = tuple(c.strip().lower()
                                         for c in _env_s("OA_PT_CLASSES",
                                                         "arb,near_arb,premium_harvest").split(",")
                                         if c.strip())
    # Stricter than scanner default — paper trader should not chase dust
    min_volume_at_entry:  float = _env_f("OA_PT_MIN_VOLUME", 100_000.0)
    # Refuse to enter if total_ask exceeds this even when scanner says premium_harvest
    max_total_ask_entry:  float = _env_f("OA_PT_MAX_ENTRY_ASK", 1.10)
    # Horizon
    min_days_to_end:      int   = _env_i("OA_PT_MIN_DAYS", 3)
    max_days_to_end:      int   = _env_i("OA_PT_MAX_DAYS", 365)

    # Exit policy (per-position, evaluated each cycle)
    # Take profit on MtM = basket_units * total_bid
    take_profit_pct:      float = _env_f("OA_PT_TP", 0.10)        # +10% MtM gain
    stop_loss_pct:        float = _env_f("OA_PT_SL", 0.20)        # -20% MtM loss
    # Don't apply stop loss to pure-arb positions (they have positive edge to settlement)
    skip_stoploss_for_arb: bool = _env_b("OA_PT_NO_SL_ON_ARB", True)
    # Force-close at MtM bid this many days before settlement, in case the
    # event linger-resolves and we want to lock in instead of waiting.
    force_close_days:     int   = _env_i("OA_PT_FORCE_CLOSE_DAYS", 0)  # 0 = always wait for settle

    # Loop cadence
    interval_sec:         int   = _env_i("OA_PT_INTERVAL", 600)        # 10 min

    # Outputs (paths relative to OA_DATA_DIR if not absolute)
    positions_path:       str   = _env_s("OA_PT_POSITIONS",  "data/paper_positions.csv")
    legs_path:            str   = _env_s("OA_PT_LEGS",       "data/paper_position_legs.csv")
    closed_path:          str   = _env_s("OA_PT_CLOSED",     "data/paper_trades_closed.csv")
    snapshots_path:       str   = _env_s("OA_PT_SNAPSHOTS",  "data/paper_position_mtm.jsonl")
    # Source of candidate event_slugs (overwritten by 01_scan_events.py)
    opportunities_path:   str   = _env_s("OA_PT_OPPS",       "data/overround_opportunities.csv")


@dataclass
class Config:
    data_dir: str = _env_s("OA_DATA_DIR", "data")
    log_level: str = _env_s("OA_LOG_LEVEL", "INFO")

    api:   ApiConfig         = field(default_factory=ApiConfig)
    scan:  ScanConfig        = field(default_factory=ScanConfig)
    paper: PaperTradeConfig  = field(default_factory=PaperTradeConfig)


def load() -> Config:
    cfg = Config()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return cfg
