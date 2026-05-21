#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/poly-live-s5

export BTC5M_OB_DB_PATH=/home/ubuntu/.local/share/arb-engine-ob-reversion/btc5m-ob-reversion.sqlite

export BTC5M_INTERVAL_SEC=10
export BTC5M_BINANCE_URL=https://api.binance.com
export BTC5M_TG_ENABLED=false

export BTC5M_OB_MIN_SECONDS_LEFT=60
export BTC5M_OB_MAX_SECONDS_LEFT=150

export BTC5M_OB_MIN_DEVIATION_BPS=6

export BTC5M_OB_MAX_ENTRY_PRICE=0.55
export BTC5M_OB_MAX_SPREAD_PCT=0.03
export BTC5M_OB_MIN_ASK_DEPTH_SHARES=10

export BTC5M_OB_MIN_TOTAL_DEPTH_SHARES=20
export BTC5M_OB_MAX_TOTAL_DEPTH_SHARES=2500

export BTC5M_OB_MAX_BID_DROP=0.02
export BTC5M_OB_MIN_BID_SIZE_RATIO=0.80

export BTC5M_OB_RET15_MIN=15
export BTC5M_OB_RET15_VETO_BPS=25

export BTC5M_OB_NOTIONAL_USD=5
export BTC5M_OB_ENTRY_SLIPPAGE=0.01
export BTC5M_OB_MAX_OPEN_POSITIONS=1

export BTC5M_SETTLE_DELAY_SEC=8
export BTC5M_VOL_LOOKBACK_MIN=120

exec /home/ubuntu/poly-live-s5/.venv/bin/python -m btc5m_strategy.arb.btc5m_orderbook_reversion_observer
