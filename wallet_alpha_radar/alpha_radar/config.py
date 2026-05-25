"""
Central config for Wallet Alpha Radar.

All values overridable via env. No third-party deps. Defaults are conservative.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_f(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default

def _env_i(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default

def _env_s(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None else default

def _env_b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass
class ApiConfig:
    # Same endpoints used by btc5m_strategy. No key needed for read-only.
    gamma_url:        str = _env_s("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob_url:         str = _env_s("POLY_CLOB_URL",  "https://clob.polymarket.com")
    data_url:         str = _env_s("POLY_DATA_URL",  "https://data-api.polymarket.com")
    leaderboard_url:  str = _env_s("POLY_LB_URL",    "https://lb-api.polymarket.com")
    # request behaviour
    timeout_sec:      float = _env_f("WAR_HTTP_TIMEOUT", 15.0)
    sleep_between:    float = _env_f("WAR_HTTP_SLEEP", 0.30)   # be polite
    max_retries:      int   = _env_i("WAR_HTTP_RETRIES", 4)
    user_agent:       str   = _env_s("WAR_USER_AGENT", "wallet-alpha-radar/0.1")


@dataclass
class DiscoveryConfig:
    # leaderboard pagination — pull deep, skip the top
    # Window values must match the upstream API: "1d" | "7d" | "30d" | "all"
    leaderboard_windows:  tuple = ("1d", "7d", "30d", "all")
    leaderboard_metric:   str   = _env_s("WAR_LB_METRIC", "profit")  # profit | volume
    leaderboard_skip_top: int   = _env_i("WAR_LB_SKIP_TOP", 20)      # avoid celebrities
    leaderboard_take:     int   = _env_i("WAR_LB_TAKE", 200)         # ranks 20..220
    # market-winner discovery: scan recently resolved markets
    winner_lookback_days: int   = _env_i("WAR_WINNER_LOOKBACK_DAYS", 30)
    winner_min_volume:    float = _env_f("WAR_WINNER_MIN_VOLUME", 5_000.0)
    winner_entry_min:     float = _env_f("WAR_WINNER_ENTRY_MIN", 0.30)
    winner_entry_max:     float = _env_f("WAR_WINNER_ENTRY_MAX", 0.65)
    winner_top_n:         int   = _env_i("WAR_WINNER_TOP_N", 10)


@dataclass
class HistoryConfig:
    # per-wallet trade pull
    trades_page_size: int = _env_i("WAR_TRADES_PAGE", 500)
    trades_hard_cap:  int = _env_i("WAR_TRADES_CAP", 5_000)
    # market metadata cache TTL (seconds)
    market_cache_sec: int = _env_i("WAR_MARKET_CACHE_SEC", 86_400)


@dataclass
class ScoringConfig:
    # ---- hard filters (drop if any fail) ----
    min_settled_trades:        int   = _env_i("WAR_MIN_SETTLED", 50)
    min_active_days:           int   = _env_i("WAR_MIN_ACTIVE_DAYS", 30)
    min_total_volume_usd:      float = _env_f("WAR_MIN_VOLUME", 3_000.0)
    max_hedge_ratio:           float = _env_f("WAR_MAX_HEDGE", 0.20)
    max_profit_concentration:  float = _env_f("WAR_MAX_CONC", 0.35)
    max_entry_090_plus_ratio:  float = _env_f("WAR_MAX_090_RATIO", 0.30)
    # ---- soft scoring ranges ----
    entry_mid_lo:    float = _env_f("WAR_ENTRY_MID_LO", 0.35)
    entry_mid_hi:    float = _env_f("WAR_ENTRY_MID_HI", 0.65)
    # tier cutoffs (on score-out-of-70 since Phase 1 omits the 30-pt follow-replicability block)
    tier_a_min:      float = _env_f("WAR_TIER_A_MIN", 50.0)
    tier_b_min:      float = _env_f("WAR_TIER_B_MIN", 38.0)


@dataclass
class RecorderConfig:
    # which token-ids to snapshot — written one per line
    watchlist_path:   str = _env_s("WAR_WATCHLIST", "data/orderbook_watchlist.txt")
    snapshot_dir:     str = _env_s("WAR_SNAPSHOT_DIR", "data/orderbook")
    interval_sec:     int = _env_i("WAR_OB_INTERVAL", 30)
    batch_size:       int = _env_i("WAR_OB_BATCH", 50)
    rotate_daily:     bool = _env_b("WAR_OB_ROTATE_DAILY", True)


@dataclass
class TradeTailConfig:
    # which wallets to tail — read from wallet_scores.csv where tier in tiers
    scores_path:      str   = _env_s("WAR_TT_SCORES", "data/wallet_scores.csv")
    output_path:      str   = _env_s("WAR_TT_OUTPUT", "data/wallet_trades_tail.csv")
    tiers:            tuple = tuple(t.strip().upper()
                                     for t in _env_s("WAR_TT_TIERS", "A").split(",")
                                     if t.strip())
    interval_sec:     int   = _env_i("WAR_TT_INTERVAL", 60)
    fetch_limit:      int   = _env_i("WAR_TT_FETCH_LIMIT", 100)


@dataclass
class BacktestConfig:
    # which trades to score — "tail" (forward-recorded), "history" (Phase-1
    # historical), or "both" (union, deduped by tx hash)
    trade_source:     str   = _env_s("WAR_BT_SOURCE", "both")
    trades_tail_path: str   = _env_s("WAR_BT_TAIL", "data/wallet_trades_tail.csv")
    history_path:     str   = _env_s("WAR_BT_HISTORY", "data/wallet_trade_history.csv")
    snapshot_dir:     str   = _env_s("WAR_BT_SNAPSHOTS", "data/orderbook")
    output_per_trade: str   = _env_s("WAR_BT_TRADES_OUT", "data/wallet_follow_simulated.csv")
    output_summary:   str   = _env_s("WAR_BT_SUMMARY_OUT", "data/wallet_follow_backtest.csv")
    # delays in seconds to simulate
    delays_sec:       tuple = tuple(int(x) for x in _env_s("WAR_BT_DELAYS", "60,300,1800").split(","))
    # copyable thresholds (the actual real-money gates we'd use later)
    max_slip_cents:   float = _env_f("WAR_BT_MAX_SLIP", 0.03)
    max_spread:       float = _env_f("WAR_BT_MAX_SPREAD", 0.05)
    min_depth_usd:    float = _env_f("WAR_BT_MIN_DEPTH", 30.0)
    # if no snapshot within this many seconds of the target time, mark as "no_snapshot"
    snap_max_gap_sec: int   = _env_i("WAR_BT_MAX_GAP", 90)
    # wallets to include (filter by tier, "*" = no filter)
    tiers:            tuple = tuple(t.strip().upper()
                                     for t in _env_s("WAR_BT_TIERS", "A").split(",")
                                     if t.strip())


@dataclass
class Config:
    data_dir: str = _env_s("WAR_DATA_DIR", "data")
    log_level: str = _env_s("WAR_LOG_LEVEL", "INFO")

    api:        ApiConfig        = field(default_factory=ApiConfig)
    discovery:  DiscoveryConfig  = field(default_factory=DiscoveryConfig)
    history:    HistoryConfig    = field(default_factory=HistoryConfig)
    scoring:    ScoringConfig    = field(default_factory=ScoringConfig)
    recorder:   RecorderConfig   = field(default_factory=RecorderConfig)
    tradetail:  TradeTailConfig  = field(default_factory=TradeTailConfig)
    backtest:   BacktestConfig   = field(default_factory=BacktestConfig)


def load() -> Config:
    cfg = Config()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return cfg
