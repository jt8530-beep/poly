# BTC5M S6 Safe Optimization Notes

This is the safety-first upgrade path for the Polymarket BTC Up/Down 5m strategy.

## What was added

File added:

- `arb/s6_safety.py`

It contains pure helper functions only. It does not submit orders and does not touch credentials.

## New safety modules

### 1. `calibrated_fair`

The original model estimates fair probability from short-term Binance BTCUSDT volatility and a normal CDF. S6 keeps that estimate but shrinks it slightly toward 50% before using it for live decisions.

Purpose:

- reduce overconfidence from the normal-distribution assumption
- make dry-run/live behavior more conservative before enough real execution data exists

Default idea:

```text
raw_fair -> raw_fair * 95% + 50% * 5%
```

### 2. `effective_edge_bps`

The old entry edge is:

```text
raw_edge = fair_probability - ask_price
```

S6 computes:

```text
effective_edge = raw_edge
               - spread penalty
               - depth penalty
               - late-entry penalty
               - model-error buffer
```

Purpose:

- avoid buying theoretical edge that disappears after spread, thin depth, late-window risk, and model error
- make Polymarket execution quality explicit instead of hidden inside paper PnL

Suggested dry-run gate:

```text
BTC5M_USE_EFFECTIVE_EDGE=1
BTC5M_MIN_EFFECTIVE_EDGE_BPS=500
```

### 3. `shock_filter_reason`

Detects abnormal 1-minute BTC moves from recent closed 1-minute closes.

It can return:

- `shock_skip`
- `vol_spike_skip`

Purpose:

- skip windows where BTC has just made an abnormal move
- avoid paying up during mini-liquidation or news spikes

Suggested defaults:

```text
BTC5M_SHOCK_FILTER_ENABLED=1
BTC5M_MAX_1M_MOVE_BPS=35
BTC5M_VOL_SPIKE_MULT=3
BTC5M_MIN_SPIKE_MOVE_BPS=20
```

### 4. `dynamic_exit_reason`

Replaces the idea of blindly forcing exit at tail60.

It returns:

- `dynamic_loss_120`
- `dynamic_loss_90`
- `dynamic_tail_liquid`
- `liquidity_trapped`

Purpose:

- only exit when there is enough bid depth
- explicitly record when the strategy wants to exit but the book is too thin
- avoid paper results pretending that an exit was possible when the live book was gone

Suggested defaults:

```text
BTC5M_DYNAMIC_EXIT_ENABLED=1
BTC5M_TAIL_FORCE_EXIT_SEC=0
BTC5M_EXIT_MIN_BID_DEPTH_SHARES=25
BTC5M_DYNAMIC_EXIT1_SEC=120
BTC5M_DYNAMIC_EXIT1_MIN_LOSS=0.15
BTC5M_DYNAMIC_EXIT2_SEC=90
BTC5M_DYNAMIC_EXIT3_SEC=60
```

## Critical bug still to patch in `btc5m_live_small.py`

Inside `_mark_positions`, add this line after `notional` is computed:

```python
seconds_left = float(row["end_epoch"]) - now_ts
```

Recommended location:

```python
entry = float(row["entry_price"])
size = float(row["size_shares"])
notional = float(row["notional_usd"])
seconds_left = float(row["end_epoch"]) - now_ts
pnl = (float(mark) - entry) * size
```

Without this patch, live-small can raise `NameError: name 'seconds_left' is not defined` when there is an open position and the mark loop reaches tail/pre-settle logic.

## Recommended S6 dry-run parameters

```bash
export BTC5M_LIVE_ENABLED=false
export BTC5M_LIVE_DRY_RUN=true

export BTC5M_MIN_EDGE_BPS=800
export BTC5M_USE_EFFECTIVE_EDGE=1
export BTC5M_MIN_EFFECTIVE_EDGE_BPS=500
export BTC5M_SPREAD_EDGE_PENALTY_MULT=0.8
export BTC5M_MODEL_ERROR_BUFFER_BPS=150
export BTC5M_DEPTH_PENALTY_MIN_SHARES=35
export BTC5M_DEPTH_PENALTY_MAX_BPS=250

export BTC5M_FAIR_SHRINK_TO_HALF=0.05

export BTC5M_SHOCK_FILTER_ENABLED=1
export BTC5M_MAX_1M_MOVE_BPS=35
export BTC5M_VOL_SPIKE_MULT=3
export BTC5M_MIN_SPIKE_MOVE_BPS=20
export BTC5M_MAX_TREND_BPS=45

export BTC5M_DYNAMIC_EXIT_ENABLED=1
export BTC5M_TAIL_FORCE_EXIT_SEC=0
export BTC5M_EXIT_MIN_BID_DEPTH_SHARES=25
export BTC5M_DYNAMIC_EXIT1_SEC=120
export BTC5M_DYNAMIC_EXIT1_MIN_LOSS=0.15
export BTC5M_DYNAMIC_EXIT2_SEC=90
export BTC5M_DYNAMIC_EXIT3_SEC=60

export BTC5M_MAX_NOTIONAL_USD=5
export BTC5M_MAX_OPEN_POSITIONS=1
```

## Go / no-go rule

Do not increase live notional until dry-run proves:

1. effective edge remains positive after penalties
2. exit-side bid depth exists near exit windows
3. `liquidity_trapped` rate is low
4. Binance proxy settlement and Polymarket final settlement are reconciled
5. rejected or failed live order rate is known

S6 is not designed to make the backtest prettier. It is designed to answer the real question:

```text
How much paper edge survives executable live conditions?
```
