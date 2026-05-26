# Overround Arb Scanner

Read-only scanner that identifies multi-outcome Polymarket events trading at
or below their guaranteed $1 settlement value (or just barely above it).

This is **Phase 1**: detect opportunities and write a CSV. No orders, no
keys, no real money. Phase 2 will be a position-builder + exit-monitor.

## Why this exists

Polymarket runs "negRisk" events — mutually-exclusive baskets where exactly
one outcome resolves to $1 and all others to $0. The sum of YES asks across
all outcomes is the cost of guaranteeing $1, and **it's frequently below
$1** (or only a few percent above) when:

- The event is early and most retail attention is on a few favorites
- Liquidity is thin on long-tail outcomes
- New outcomes were added (e.g., wildcard teams) but no one bid them down

Two playable shapes:

| Class             | total_ask  | What it is                                     |
|-------------------|------------|------------------------------------------------|
| `arb`             | < $1.00    | Pure settlement arb. Buy basket, hold to settle, profit guaranteed. |
| `near_arb`        | $1.00–1.05 | Cheap basket. Sell positions back into demand as event approaches. |
| `premium_harvest` | $1.05–1.20 | Same idea but needs a longer horizon for premium to grow. |

The famous gucky-gu45 case (48 World Cup teams, $50.5k in, ~$100k out) was
a `near_arb` / `premium_harvest` play executed before the tournament. He
bought when the basket was cheap and exited as the total ask inflated past
$1. This scanner finds those baskets.

## What it does NOT do

- **Trade.** Read-only. Hand-pick what looks interesting and trade
  manually for now.
- **Account for transaction costs / gas / API fees.** A printed `arb` of
  $0.005 will be eaten by gas. Treat the threshold conservatively.
- **Verify orderbook depth beyond best ask.** A printed total_ask is the
  top-of-book price; you may not be able to buy meaningful size at it.
  `total_bid` and per-event liquidity in the CSV are the sanity check.
- **Filter for hot vs cold liquidity.** A market with 30 days of life left
  and $30M volume is very different from one with 200 days and $50k.
  Look at `days_to_end`, `volume`, `liquidity` together.

## Layout

```
overround_arb/
├── README.md
├── .env.example
├── requirements.txt
├── overround_arb/
│   ├── __init__.py
│   ├── config.py    env-driven config dataclasses
│   ├── api.py       minimal stdlib HTTP client for gamma /events
│   └── util.py      logging + CSV helpers
├── scripts/
│   ├── 00_health_check.py   diagnostic
│   └── 01_scan_events.py    main scanner — one-shot
└── data/
    ├── overround_opportunities.csv   latest snapshot (overwritten each run)
    └── scan_history.csv              append mode (only if OA_APPEND_HISTORY=1)
```

## Run

```bash
cd overround_arb
cp .env.example .env
# tweak thresholds in .env if you want, defaults are fine

python3 scripts/01_scan_events.py
column -ts, data/overround_opportunities.csv | head -30
```

Typical wall-clock: 5-15 seconds depending on `OA_EVENTS_MAX_PAGES`.

To track opportunities over time (recommended — premium_harvest plays
play out over weeks), run on cron with history mode on:

```cron
# every 30 minutes
*/30 * * * * cd /opt/poly/overround_arb && OA_APPEND_HISTORY=1 \
    python3 scripts/01_scan_events.py >> /var/log/overround_arb.log 2>&1
```

Then check progress any time:

```bash
python3 scripts/00_health_check.py
```

## Output schema (`data/overround_opportunities.csv`)

| column                    | meaning                                                    |
|---------------------------|------------------------------------------------------------|
| `scan_ts`                 | UTC ISO8601 — when this snapshot was taken                 |
| `classification`          | `arb` / `near_arb` / `premium_harvest` / `overpriced` / `anomalous` |
| `event_id`, `event_slug`  | gamma identifiers                                          |
| `title`                   | human-readable event name                                  |
| `n_markets_total`         | every market gamma reports for the event                   |
| `n_markets_active`        | markets contributing to total_ask (open + bestAsk in (0, 0.99)) |
| `n_markets_eliminated`    | excluded — closed, zero ask, or placeholder ask >= 0.99    |
| `total_ask`               | Σ bestAsk over active markets — the cost of buying the basket  |
| `total_bid`               | Σ bestBid — what you'd recover selling the basket immediately  |
| `edge_usd_per_dollar`     | `1 - total_ask`. Positive = pure arb edge per $1 staked    |
| `spread`                  | `total_ask - total_bid`                                    |
| `volume`, `liquidity`     | event lifetime $ — context for whether you can size in/out |
| `end_date`, `days_to_end` | settlement timing                                          |
| `top_outcome_*`, `second_*` | favorites (most expensive YES asks) — gives a sense of the field |
| `negRiskMarketID`         | on-chain neg-risk market identifier                        |

## Classification thresholds (env-overridable)

| env var               | default  | meaning                                            |
|-----------------------|----------|----------------------------------------------------|
| `OA_ARB_MAX`          | 1.00     | total_ask < this → `arb`                           |
| `OA_NEAR_ARB_MAX`     | 1.05     | total_ask < this → `near_arb`                      |
| `OA_PREMIUM_MAX`      | 1.20     | total_ask < this → `premium_harvest` (with horizon) |
| `OA_PREMIUM_MIN_DAYS` | 30       | minimum days_to_end for `premium_harvest`          |
| `OA_SANITY_MIN/MAX`   | 0.5/1.5  | totals outside this range get flagged `anomalous`  |

Anomalous events are usually contaminated with prop markets the scanner
couldn't filter out. Inspect manually before betting on them.

## Roadmap (Phase 2+)

Not in this PR — flagged here so the design constraints are visible:

1. **`02_track_opportunity.py`** — given an `event_slug`, watch its total_ask
   over time at higher cadence (every 30s) and chart the entry zone.
2. **`03_position_builder.py`** — for a hand-picked event, compute the
   exact buy quantities per outcome to lock in `edge_usd_per_dollar` with
   a $X budget, accounting for orderbook depth.
3. **`04_exit_monitor.py`** — watch a held basket and signal individual
   outcome sells when local mark-to-market crosses target.
4. **CLOB write client + signing** — actually place orders.

The Phase 2 trio plus signing is what turns the scanner into a trader.
Don't build it until you've watched a few `near_arb` plays evolve over a
few weeks via the scan_history.csv and have a feel for the dynamics.
