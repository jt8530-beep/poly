"""
Config for LP Market Maker scanner. Env-driven, stdlib only.

Same _clean() trick used elsewhere in this repo: strips inline `#` comments
defensively so the same .env file works whether sourced via systemd, docker
--env-file, dotenv, etc.

This module ALSO loads `<package>/../.env` at import time so that values in
.env are honored when scripts are run directly via `python3 scripts/...`.
Without this, only systemd's EnvironmentFile would actually source the file
and `os.getenv()` would silently fall back to defaults — which is the bug
that produced the 2026-05-26 PLTR-loss postmortem (whitelist filter never
took effect).
"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# .env loader (called at MODULE level — see comment below)
# ---------------------------------------------------------------------------
def _load_dotenv() -> int:
    """
    Read `<package>/../.env` (e.g. `lp_market_maker/.env`) and inject any
    KEY=VALUE pairs into os.environ that aren't already set there.

    `not already set` is critical: if the process was launched by systemd
    with `EnvironmentFile=`, those values are already in os.environ and we
    must not overwrite them. This way the same code works for:
      * `python3 scripts/X.py`  → loads .env from this loader
      * `systemctl start X`     → loads .env via systemd, this loader is no-op

    Returns the count of vars actually injected (for diagnostics).
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return 0
    n = 0
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            key, _, val = s.partition("=")
            key = key.strip()
            val = val.strip()
            # strip surrounding quotes if present
            if len(val) >= 2 and (
                (val.startswith('"') and val.endswith('"')) or
                (val.startswith("'") and val.endswith("'"))):
                val = val[1:-1]
            # do NOT override existing env (systemd-sourced values win)
            if key and key not in os.environ:
                os.environ[key] = val
                n += 1
    except Exception as e:                                        # noqa: BLE001
        logging.getLogger("lp.config").warning(".env load failed: %s", e)
    return n


# CRITICAL: this MUST run before the @dataclass class bodies below are
# evaluated, because dataclass field defaults like `_env_s("LP_X", "")` are
# computed at class-definition time (i.e. at import). If we deferred this
# to load() like a naive impl, the env values would be loaded too late.
_LOADED_DOTENV_COUNT = _load_dotenv()


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


@dataclass
class ApiConfig:
    gamma_url:    str   = _env_s("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob_url:     str   = _env_s("POLY_CLOB_URL",  "https://clob.polymarket.com")
    timeout_sec:  float = _env_f("LP_HTTP_TIMEOUT", 15.0)
    sleep_between: float = _env_f("LP_HTTP_SLEEP", 0.20)
    max_retries:  int   = _env_i("LP_HTTP_RETRIES", 4)
    user_agent:   str   = _env_s("LP_USER_AGENT", "lp-market-maker/0.1")


@dataclass
class ScanConfig:
    # Pull this many event pages — 100/page = 1000 events covered with 10
    events_max_pages: int = _env_i("LP_EVENTS_MAX_PAGES", 15)
    events_page_size: int = _env_i("LP_EVENTS_PAGE", 100)

    # Market eligibility filters
    # Reward pool floor — ignore dust pools
    min_daily_rate_usd:   float = _env_f("LP_MIN_DAILY_RATE", 30.0)
    # Skip markets whose mid is in extreme territory — adverse selection risk
    # at 0.0X or 0.9X is much higher than at 0.4–0.6 because tail events are
    # typically information-driven.
    min_mid_price:        float = _env_f("LP_MIN_MID", 0.005)
    max_mid_price:        float = _env_f("LP_MAX_MID", 0.95)
    # Need ask + bid both populated (some real liquidity)
    require_both_sides:   bool  = _env_s("LP_BOTH_SIDES", "1") in ("1", "true", "yes")
    # Time-to-end window. Don't deploy on markets resolving too soon (you'd
    # eat resolution risk before earning much reward).
    min_hours_to_end:     float = _env_f("LP_MIN_HOURS", 1.0)
    # Don't bother with infinite-horizon markets either — capital efficiency
    # drops as horizon grows since you can't easily redeploy.
    max_days_to_end:      float = _env_f("LP_MAX_DAYS", 365.0)

    # ----- v2: tag-based filtering (LESSON FROM 2026-05-26 PLTR LOSS) -----
    # Tags whose presence flag a market as "underlying-volatility-driven" — even
    # if scanner shows wide spread + thin competition, the spread isn't
    # opportunity, it's a risk premium. PLTR/AAPL/EWY-style equity-derivative
    # markets bleed inventory overnight on any earnings/news/macro move; we
    # paid \$5.90 to learn this. See POSTMORTEM.md for the full breakdown.
    blacklist_tags:       tuple = tuple(t.strip() for t in _env_s("LP_BLACKLIST_TAGS",
        "Finance,Stocks,Equity,ETF,ETFs,Macro,Crypto Prices,Hit Price,Politics,"
        "Election,Elections,FX,Forex,Bonds,Commodities,Energy,Interest Rates"
    ).split(",") if t.strip())
    # If non-empty, ONLY keep markets whose tags include at least one of these.
    # Default empty (=permissive) but recommended setting:
    #   Esports,Games,Sports,Weather
    whitelist_tags:       tuple = tuple(t.strip() for t in _env_s("LP_WHITELIST_TAGS", "").split(",") if t.strip())
    # If 1, hard-skip events whose volume is too high for "dead zone" plays.
    # Big-volume events typically have professional MMs in them.
    max_event_volume_usd: float = _env_f("LP_MAX_EVENT_VOLUME", 0.0)  # 0 = no cap

    # Suggested order parameters (output, used to estimate capital required)
    # We'll suggest placing orders within this fraction of rewardsMaxSpread
    # toward the mid. 0.9 = "just inside the reward band" so we capture
    # nearly all the proximity score without being so aggressive we get
    # hit constantly.
    inside_band_factor:   float = _env_f("LP_INSIDE_BAND", 0.9)
    # We always quote at least rewardsMinSize. If user wants bigger, scale up.
    size_multiplier:      float = _env_f("LP_SIZE_MULT", 1.0)

    # Heuristic competition capture (rough — refined empirically once we
    # have real data from manual deploys).
    # If observed top-of-book spread > N × max_reward_spread, treat as
    # "no competition" and assume we capture ~capture_alone of the pool.
    capture_alone_factor:    float = _env_f("LP_CAPTURE_ALONE", 0.7)
    capture_competitive:     float = _env_f("LP_CAPTURE_COMPETITIVE", 0.15)
    # threshold for "wide spread = sparse competition"
    sparse_spread_multiple:  float = _env_f("LP_SPARSE_MULT", 3.0)

    # Output ranking — top-N opportunities to surface
    top_n:               int   = _env_i("LP_TOP_N", 20)


@dataclass
class Config:
    data_dir: str = _env_s("LP_DATA_DIR", "data")
    log_level: str = _env_s("LP_LOG_LEVEL", "INFO")

    api:  ApiConfig  = field(default_factory=ApiConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)


def load() -> Config:
    cfg = Config()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    if _LOADED_DOTENV_COUNT:
        logging.getLogger("lp.config").info(
            ".env loaded: %d vars from %s",
            _LOADED_DOTENV_COUNT,
            Path(__file__).resolve().parent.parent / ".env",
        )
    return cfg
