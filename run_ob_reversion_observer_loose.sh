#!/usr/bin/env bash
# OB LOOSE — relaxed orderbook reversion observer
# Same code as strict OB, lower thresholds for more sample volume
set -euo pipefail

cd /home/ubuntu/poly-live-s5

export BTC5M_OB_DB_PATH=/home/ubuntu/.local/share/arb-engine-ob-reversion/btc5m-ob-reversion-loose.sqlite

export BTC5M_INTERVAL_SEC=10
export BTC5M_BINANCE_URL=https://api.binance.com
export BTC5M_TG_ENABLED=false

export BTC5M_OB_MIN_SECONDS_LEFT=60
export BTC5M_OB_MAX_SECONDS_LEFT=150

# ── LOOSE: lower deviation threshold ──
export BTC5M_OB_MIN_DEVIATION_BPS=4

export BTC5M_OB_MAX_ENTRY_PRICE=0.60
export BTC5M_OB_MAX_SPREAD_PCT=0.04
export BTC5M_OB_MIN_ASK_DEPTH_SHARES=10

# ── LOOSE: wider depth range ──
export BTC5M_OB_MIN_TOTAL_DEPTH_SHARES=10
export BTC5M_OB_MAX_TOTAL_DEPTH_SHARES=4000

export BTC5M_OB_MAX_BID_DROP=0.03
export BTC5M_OB_MIN_BID_SIZE_RATIO=0.70

# ── LOOSE: softer ret15 veto ──
export BTC5M_OB_RET15_MIN=10
export BTC5M_OB_RET15_VETO_BPS=35

export BTC5M_OB_NOTIONAL_USD=5
export BTC5M_OB_ENTRY_SLIPPAGE=0.01
export BTC5M_OB_MAX_OPEN_POSITIONS=1

export BTC5M_SETTLE_DELAY_SEC=8
export BTC5M_VOL_LOOKBACK_MIN=120

exec /home/ubuntu/poly-live-s5/.venv/bin/python -m btc5m_strategy.arb.btc5m_orderbook_reversion_observer
