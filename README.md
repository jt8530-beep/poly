# poly

Repository for two Polymarket workstreams:

- `honest_backtest/`: a separate analysis path for the original viral-tweet capital / backtest work
- `btc5m_strategy/`: the current BTC 5-minute paper strategy, parameter template, and notes

## What matters now

If you only care about the live BTC 5m direction, start here:

- `btc5m_strategy/README.md`
- `btc5m_strategy/current_strategy.env.example`

## BTC 5m status

The current live paper version uses:

1. Trend gate first
2. Chosen-side entry exitability filter second
3. Edge filter third
4. Tail force exit near settlement

This repository keeps the strategy notes and the parameter template in sync with that live setup.
