# LP Market Maker (Phase 0 scanner)

Read-only scanner that finds Polymarket markets where you can dominate
the LP reward pool with very little capital — the gucky-gu45 / OP esports
"挂在死区拿做市奖励" play.

## How it works

Polymarket pays daily rewards to LPs whose orders sit at top-of-book within
`rewardsMaxSpread` of the market mid. The reward share goes to:

```
your_score = your_size × proximity_to_mid × time_at_top_of_book
            (over orders within rewardsMaxSpread of mid)
your_share = your_score / Σ(all_LPs' scores in this market)
```

In thin markets (esports mid-game breaks, off-hours geo events), there
are often **zero competitors** in the reward band. Place a bid + ask
just inside `rewardsMaxSpread` of mid with `rewardsMinSize` shares, and
you capture most of the day's pool against $5-$50 in capital.

This scanner finds those markets. It does NOT trade.

## Phase 0 / Phase 1 split

| Phase | What | Risk |
|---|---|---|
| **0 (this PR)** | Scanner only. You manually place orders on the Polymarket UI. | Capital you decide to deploy |
| **1 (next)** | CLOB write client + EIP-712 signing → automated bot | Whatever USDC you deposit |

Phase 0 is a 1-night verdict: deploy ~$50-150 manually tonight, wake up,
check what you actually earned. If LP_reward > inventory_loss, build
Phase 1. Otherwise stop and learn why.

## Layout

```
lp_market_maker/
├── README.md
├── .env.example
├── requirements.txt
├── lp_market_maker/
│   ├── __init__.py
│   ├── config.py    env-driven config
│   ├── api.py       gamma /events client
│   └── util.py      logging + CSV
├── scripts/
│   └── 01_scan_lp.py    main scanner
└── data/
    └── lp_opportunities.csv   ranked output (overwritten each run)
```

## Run

```bash
cd lp_market_maker
cp .env.example .env

python3 scripts/01_scan_lp.py
column -ts, data/lp_opportunities.csv | head -25
```

Wall-clock: ~10 seconds.

## Output schema

Each row is one LP opportunity. Sorted by `roi_per_day` descending.

| column                       | meaning                                                  |
|------------------------------|----------------------------------------------------------|
| `event_slug`                 | gamma slug — paste into Polymarket URL to inspect        |
| `market_question`            | "Will Team X win Game 3?" etc.                           |
| `outcome_title`              | YES outcome name (team / option being bet on)            |
| `yes_token_id`               | CLOB token id for YES side                               |
| `best_bid`, `best_ask`, `mid`| current top-of-book                                      |
| `current_spread`             | ask - bid                                                |
| `rewards_daily_rate_usd`     | daily reward pool ($)                                    |
| `rewards_max_spread_cents`   | max distance from mid (cents) for orders to count        |
| `rewards_min_size`           | min order size (shares) to qualify                       |
| `competition`                | `sparse` / `moderate` / `competitive`                    |
| `estimated_capture_pct`      | what fraction of pool we likely capture                  |
| `estimated_daily_reward_usd` | daily_rate × capture                                     |
| `suggested_bid_price`        | place a bid at this price                                |
| `suggested_ask_price`        | place an ask at this price                               |
| `suggested_size_shares`      | how many shares for each side                            |
| `capital_inventory_usd`      | $ to buy that many shares at current ask (for the ask)   |
| `capital_bid_max_usd`        | max $ at risk if your bid fills                          |
| `total_capital_usd`          | sum                                                      |
| `roi_per_day`                | est_daily_reward / total_capital                         |
| `hours_to_end`               | settlement timing                                        |
| `tags`                       | event tags (helpful: "Earn 4%" means reward-active)      |

## How to use the output (Phase 0 manual deploy)

1. Sort `lp_opportunities.csv` by `roi_per_day`.
2. Pick the top 3 rows where:
   - `competition` is `sparse`
   - `hours_to_end` is between 4 and 72
   - `total_capital_usd` ≤ $50
   - `tags` does NOT include politically-hot or news-driven keywords
3. For each, open `https://polymarket.com/event/<event_slug>` in a browser.
4. On the YES side, place:
   - A LIMIT BUY at `suggested_bid_price` for `suggested_size_shares`
   - A LIMIT BUY at current `best_ask` for `suggested_size_shares` (gets you inventory)
   - Then immediately place a LIMIT SELL at `suggested_ask_price` for the
     same `suggested_size_shares` (sells the inventory you just bought)
5. Total cap exposure in step 4 ≈ `total_capital_usd` per market. With 3
   markets at ~$50 each you're ~$150 in.
6. Sleep. Tomorrow check `https://polymarket.com/profile/<your address>`
   for fills + the rewards page for daily LP earnings.

## Hard rules for first deploys

- **≤ $50 per market** (adverse-selection cap)
- **≤ 3 markets** (don't over-spread your attention on first run)
- **No markets resolving in < 4h** (don't get caught by sudden settle)
- **No politically hot markets** (informed flow will pick you off)
- **Esports mid-game / off-hours geo events preferred** (truly dead)

## What the scanner can't tell you (open questions for tomorrow's tracker)

1. Are there real LP rewards being credited day-to-day, and how much?
   `02_track_my_orders.py` (next deliverable) will pull your address's
   actual reward payouts and compare to the scanner's estimate.
2. Do you eat adverse selection in practice? The tracker computes
   `(realized inventory PnL) + (LP rewards)` so you see the net.
3. Is the reward formula approximated correctly? Calibrate
   `LP_CAPTURE_ALONE` and `LP_CAPTURE_COMPETITIVE` from real data.

After 1-2 nights of real data we know whether to build Phase 1.
