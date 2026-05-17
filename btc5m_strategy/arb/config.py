"""
Central config. Values override from env vars.

Keep it boring and explicit. No dependencies on any .env parser so it runs
anywhere (VPS, local, docker) without extras.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field


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
class RiskConfig:
    # hard caps (USD in USDC terms)
    max_notional_per_trade: float = _env_f("RISK_MAX_NOTIONAL_PER_TRADE", 30.0)
    max_open_notional:      float = _env_f("RISK_MAX_OPEN_NOTIONAL", 200.0)
    max_daily_new_trades:   int   = _env_i("RISK_MAX_DAILY_NEW_TRADES", 20)
    # account circuit breakers (fractions of peak)
    soft_drawdown_stop: float = _env_f("RISK_SOFT_DD", 0.20)   # derisk
    hard_drawdown_stop: float = _env_f("RISK_HARD_DD", 0.35)   # halt
    # minimum edge after fees+gas, below this we skip
    min_edge_bps: int = _env_i("RISK_MIN_EDGE_BPS", 150)       # 1.50%


@dataclass
class PolyConfig:
    # official endpoints, no key needed for read
    gamma_url: str = _env_s("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob_url:  str = _env_s("POLY_CLOB_URL",  "https://clob.polymarket.com")
    data_url:  str = _env_s("POLY_DATA_URL",  "https://data-api.polymarket.com")
    # your wallet (only needed when we switch from paper -> live)
    polygon_private_key: str = _env_s("POLYGON_PRIVATE_KEY", "")
    proxy_wallet_address: str = _env_s("POLY_PROXY_WALLET", "")
    # L2 API creds (derived once via CLOB /auth/api-key)
    clob_api_key:    str = _env_s("POLY_CLOB_API_KEY", "")
    clob_api_secret: str = _env_s("POLY_CLOB_API_SECRET", "")
    clob_api_passphrase: str = _env_s("POLY_CLOB_API_PASSPHRASE", "")


@dataclass
class TelegramConfig:
    bot_token: str = _env_s("TG_BOT_TOKEN", "")
    chat_id:   str = _env_s("TG_CHAT_ID", "")
    enabled:   bool = _env_b("TG_ENABLED", True)


@dataclass
class EngineConfig:
    paper_mode: bool = _env_b("PAPER_MODE", True)            # start paper
    scan_interval_sec: int = _env_i("SCAN_INTERVAL_SEC", 30)
    ladder_min_violation_bps: int = _env_i("LADDER_MIN_VIOLATION_BPS", 200)   # 2%
    ladder_min_depth_usd: float = _env_f("LADDER_MIN_DEPTH_USD", 20.0)
    db_path: str = _env_s("DB_PATH", "ledger.sqlite")
    log_level: str = _env_s("LOG_LEVEL", "INFO")

    risk: RiskConfig = field(default_factory=RiskConfig)
    poly: PolyConfig = field(default_factory=PolyConfig)
    tg:   TelegramConfig = field(default_factory=TelegramConfig)


def load() -> EngineConfig:
    return EngineConfig()
