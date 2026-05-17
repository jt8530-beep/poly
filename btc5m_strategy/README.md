# BTC5m Strategy

Current live paper strategy for Polymarket `BTC Up/Down 5m`.

The live version is layered:

1. Trend gate first
   - 2-minute BTC momentum chooses `UP`, `DOWN`, or skip
   - If the trend is `UP`, only the `UP` side is eligible
   - If the trend is `DOWN`, only the `DOWN` side is eligible

2. Entry exitability second
   - chosen-side `spread_pct <= 1.45%`
   - chosen-side `ask_size >= 25`

3. Edge gate third
   - `edge >= 500 bps`
   - normal price / spread / depth checks still apply

4. Exit control last
   - `tail_force_exit_sec = 60`
   - pre-settle loss ladder stays on
   - `take_profit = 20%`
   - `stop_loss` is disabled by default in the current live setup

## Current live parameters

- `BTC5M_TREND_GATE_ENABLED=1`
- `BTC5M_TREND_LOOKBACK_MIN=2`
- `BTC5M_TREND_THRESHOLD_BPS=0`
- `BTC5M_ENTRY_EXITABILITY_MAX_SPREAD_PCT=0.0145`
- `BTC5M_ENTRY_EXITABILITY_MIN_ASK_DEPTH_SHARES=25`
- `BTC5M_TAIL_FORCE_EXIT_SEC=60`
- `BTC5M_MIN_SECONDS_AFTER_START=60`
- `BTC5M_MIN_SECONDS_BEFORE_END=45`
- `BTC5M_MIN_EDGE_BPS=500`
- `BTC5M_MAX_SPREAD_PCT=0.03`
- `BTC5M_MAX_ENTRY_PRICE=0.82`
- `BTC5M_MIN_ASK_DEPTH_SHARES=10`
- `BTC5M_MAX_OPEN_POSITIONS=1`
- `BTC5M_MAX_NOTIONAL_USD=10`
- `BTC5M_MIN_NOTIONAL_USD=5`
- `BTC5M_PRE_SETTLE_LOSS_CAP_ENABLED=1`

## Notes

This is still paper-test work. The combined version is the first version that matches the current live Oracle setup.

The main open question is still the tail. The combined setup improves average results, but the worst single trade can still get close to `-10` if the market collapses too late.
