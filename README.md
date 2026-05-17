# poly

Repository for two Polymarket workstreams:

- `honest_backtest/`: a separate analysis path for the original viral-tweet capital / backtest work
- `btc5m_strategy/`: the current BTC 5-minute paper strategy, parameter template, and notes

## What matters now

If you only care about the live BTC 5m direction, start here:

- `btc5m_strategy/README.md`
- `btc5m_strategy/current_strategy.env.example`

## BTC 5m status

The current BTC 5m version uses the more conservative S5 family:

1. 2-minute trend gate chooses the eligible side.
2. S5 layered direction filter confirms the move.
3. Edge, price, spread, depth, and time-window filters gate entries.
4. Tail force exit closes before settlement.

This repository keeps the strategy notes and the parameter template in sync with that live setup.
