# BTC5m Strategy

Current BTC 5m strategy for Polymarket `BTC Up/Down 5m`.

The active paper version is `S5 + trend gate + tail60`.
The Ireland live-small service has the same strategy code and parameters, but is currently kept in dry-run until the Polymarket deposit-wallet order path is confirmed.

1. Trend gate first
   - 2-minute BTC momentum chooses `UP`, `DOWN`, or skip
   - If the trend is `UP`, only the `UP` side is eligible
   - If the trend is `DOWN`, only the `DOWN` side is eligible

2. S5 layered direction filter
   - `edge < 1000`: require direction `>= 4 bps`
   - `edge >= 1000`: require direction `>= 2 bps`

3. Edge gate third
   - `edge >= 800 bps`
   - normal price / spread / depth checks still apply

4. Exit control last
   - `tail_force_exit_sec = 60`
   - stop loss is enabled at `30%`
   - disaster trailing exit is enabled only after large unrealized profit

## Current live parameters

- `BTC5M_TREND_GATE_ENABLED=1`
- `BTC5M_TREND_LOOKBACK_MIN=2`
- `BTC5M_TREND_THRESHOLD_BPS=0`
- `BTC5M_LOW_EDGE_CONFIRM_MAX_BPS=1000`
- `BTC5M_LOW_EDGE_MIN_DIRECTION_BPS=4`
- `BTC5M_HIGH_EDGE_MIN_DIRECTION_BPS=2`
- `BTC5M_TAIL_FORCE_EXIT_SEC=60`
- `BTC5M_MIN_SECONDS_AFTER_START=90`
- `BTC5M_MIN_SECONDS_BEFORE_END=90`
- `BTC5M_MIN_EDGE_BPS=800`
- `BTC5M_MAX_SPREAD_PCT=0.03`
- `BTC5M_MAX_ENTRY_PRICE=0.82`
- `BTC5M_MIN_ASK_DEPTH_SHARES=10`
- `BTC5M_MAX_OPEN_POSITIONS=1`
- `BTC5M_MAX_NOTIONAL_USD=10`
- `BTC5M_MIN_NOTIONAL_USD=5`
- `BTC5M_PRE_SETTLE_LOSS_CAP_ENABLED=0`
- `BTC5M_STOP_LOSS_ENABLED=1`
- `BTC5M_STOP_LOSS=0.30`
- `BTC5M_DISASTER_TRAIL_ENABLED=1`

## Notes

The long backtest favored S5 as a stable core:

- `S5_original`: total PnL `+7024.50`, validation PnL `+1166.55`, validation win rate `74.65%`
- `S5_trend_first_tail60`: total PnL `+7327.17`, validation PnL `+1142.29`, validation win rate `76.14%`, max loss improved from `-10.00` to `-9.73`

So the current version favors smoother behavior and higher win rate over the highest validation PnL.

The entry-exitability hard filter is intentionally not part of this strategy version because the long historical backtest does not have full historical order-book spread/depth data.
