#!/usr/bin/env bash
# S9C_SYDNEY_DIRECTION_TIMEBOX_V1
# S9 + direction timebox: UTC 13-20 ban DOWN, skip abs(dir)<2
set -euo pipefail

cd /home/ubuntu/poly-live-s5

export BTC5M_LIVE_DB_PATH=/home/ubuntu/.local/share/arb-engine-live-s9c/btc5m-s9c-sydney.sqlite

export BTC5M_LIVE_ENABLED=false
export BTC5M_LIVE_DRY_RUN=true
export BTC5M_TG_ENABLED=false

# ── S5 small_sydney base ──
export BTC5M_INTERVAL_SEC=10
export BTC5M_MAX_OPEN_POSITIONS=1
export BTC5M_MAX_NOTIONAL_USD=5
export BTC5M_MIN_NOTIONAL_USD=5
export BTC5M_MIN_EDGE_BPS=500
export BTC5M_MIN_DIRECTION_BPS=4
export BTC5M_MAX_SPREAD_PCT=0.03
export BTC5M_MAX_ENTRY_PRICE=0.82
export BTC5M_MIN_ASK_DEPTH_SHARES=10
export BTC5M_MIN_SECONDS_AFTER_START=60
export BTC5M_MIN_SECONDS_BEFORE_END=45
export BTC5M_TREND_GATE_ENABLED=true
export BTC5M_TREND_LOOKBACK_MIN=2
export BTC5M_TREND_THRESHOLD_BPS=0
export BTC5M_VOL_LOOKBACK_MIN=120
export BTC5M_MIN_VOL_1M=0.00035
export BTC5M_ENTRY_EXITABILITY_MAX_SPREAD_PCT=0.0145
export BTC5M_ENTRY_EXITABILITY_MIN_ASK_DEPTH_SHARES=25

# ── S9: time gate (Beijing 16:00 - 08:00) ──
export BTC5M_TIME_GATE_ENABLED=true
export BTC5M_TIME_GATE_START_UTC=8
export BTC5M_TIME_GATE_END_UTC=24

# ── S9: hold-to-expiry only ──
export BTC5M_STOP_LOSS_ENABLED=false
export BTC5M_TAIL_FORCE_EXIT_SEC=0
export BTC5M_PRE_SETTLE_LOSS_CAP_ENABLED=false
export BTC5M_DISASTER_TRAIL_ENABLED=false
export BTC5M_SETTLE_DELAY_SEC=8

# ── S9C: direction timebox ──
export BTC5M_DIRECTION_TIMEBOX_ENABLED=true
export BTC5M_S9C_MIN_ABS_DIRECTION_BPS=2

export BTC5M_BINANCE_URL=https://api.binance.com

exec /home/ubuntu/poly-live-s5/.venv/bin/python -m btc5m_strategy.arb.btc5m_live_s9c
