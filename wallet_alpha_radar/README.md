# Wallet Alpha Radar

Polymarket smart-money discovery + scoring pipeline.

This is **Phase 1**: offline discovery, history, scoring + a forward-running
orderbook recorder. Phase 2 (delay-replicated PnL backtest, paper-follow)
needs ~3-4 weeks of orderbook data first — start the recorder now.

> Why this exists: a high-PnL wallet is not enough. The only thing that
> matters is whether you can still profit if you copy it 60s / 300s / 1800s
> later. Phase 1 narrows the universe to wallets worth recording. Phase 2
> tests whether they're actually copyable.

---

## What it does

```
01_discover_candidates.py   pull leaderboard pages (skip top 20)
                            + (optional) enumerate recently-closed markets
                            → data/candidate_wallets.csv

02_build_history.py         per wallet: pull trades, fetch market metadata,
                            normalize, write flat history rows
                            → data/wallet_trade_history.csv
                            → data/markets_cache.json

03_score_wallets.py         compute metrics, apply hard filters, score (out
                            of 70), assign A / B / C tier
                            → data/wallet_scores.csv

04_record_orderbook.py      long-running. Snapshot top-of-book for a
                            watchlist of token_ids every 30s.
                            → data/orderbook/YYYY-MM-DD.jsonl
```

## Why "out of 70" not "out of 100"

The original scoring rubric reserves 30 points for `follow_300s_pnl > 0` and
similar delay-replicated metrics. Those need historical orderbook snapshots
that Polymarket does **not** publish. We can only earn those points after
recording our own orderbook for several weeks. Phase 1 stops at 70.

---

## Hard filters (drop a wallet outright)

| filter                       | env var                | default |
|------------------------------|------------------------|---------|
| settled_trades >= N          | WAR_MIN_SETTLED        | 50      |
| active_days >= N             | WAR_MIN_ACTIVE_DAYS    | 30      |
| total_volume_usd >= $        | WAR_MIN_VOLUME         | 3000    |
| hedge_ratio < x              | WAR_MAX_HEDGE          | 0.20    |
| profit_concentration < x     | WAR_MAX_CONC           | 0.35    |
| entry_090_plus_ratio < x     | WAR_MAX_090_RATIO      | 0.30    |
| trade in last 30d            | (always on)            | —       |

Anything failing a hard filter is forced to tier C regardless of score.

---

## Scoring breakdown (Phase 1 max = 70)

| component              | max | what it rewards                                       |
|------------------------|-----|-------------------------------------------------------|
| pnl_stability          | 20  | total > 0, last-30d > 0, drawdown < 35%, conc < 35%  |
| entry_quality          | 15  | weighted-median entry in 0.35–0.65, low 0.90+ ratio   |
| hedge_clean            | 10  | low YES+NO double-buy ratio (drops market makers)     |
| category               | 15  | top category has >= 30 settled trades, positive ROI   |
| liquidity_proxy        | 10  | median trade USD size, total volume thresholds        |

Tiers (after hard-filter):
- **A**: score >= `WAR_TIER_A_MIN` (50) AND last_30d_pnl > 0
- **B**: score >= `WAR_TIER_B_MIN` (38)
- **C**: everything else

---

## Run on a server

```bash
git clone <repo>
cd poly/wallet_alpha_radar
cp .env.example .env
# edit .env if you want to tighten or loosen filters

# discovery → history → scoring (the offline pipeline)
python3 scripts/01_discover_candidates.py
python3 scripts/02_build_history.py
python3 scripts/03_score_wallets.py

# inspect the output
column -ts, -n data/wallet_scores.csv | head -30
```

The first end-to-end run takes ~1-2 hours depending on how many candidates
you're pulling and how aggressive `WAR_HTTP_SLEEP` is. The data API rate-
limits, so don't lower the sleep below 0.2s.

### Re-running

- Re-running `01` appends new rows; the wallet set is deduped downstream.
- `02` is idempotent — by default it truncates and rebuilds. Set
  `WAR_HISTORY_SKIP_EXISTING=1` to skip wallets you've already fetched.
- `03` always rewrites `wallet_scores.csv` from history.

### Watchlist for the recorder

After `03_score_wallets.py` you'll have a list of A-tier wallets. To set up
the recorder for Phase 2, manually populate the watchlist with the
`clobTokenIds` of the markets those wallets are *currently* active in
(use the trade history CSV — pick markets with `resolved=0` and recent ts).

```bash
# example watchlist file
cat > data/orderbook_watchlist.txt <<'EOF'
# Markets that A-tier wallet 0xabc... is currently in
21742633143463906290569050155826241533067272736897614950488156847949938836455
48331043336612883890938759509493159234755048973500640148014422747788308965732
EOF
```

The recorder hot-reloads this file on the next round when its mtime changes.

### Long-running recorder (systemd)

A unit file is provided at `wallet_alpha_radar.service` (sibling of the
existing `arb-btc5m-*.service` files). Adjust paths and install with:

```bash
sudo cp wallet_alpha_radar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wallet_alpha_radar
journalctl -u wallet_alpha_radar -f
```

---

## What's NOT in Phase 1 (intentional)

- Per-trade winner attribution from the closed-markets sidecar
  (`data/closed_markets_todo.csv` is written but not consumed)
- `wallet_follow_backtest.py` — needs orderbook history (see recorder)
- `wallet_live_paper_follow.py` — needs the backtest first
- "Price moved with them" detection — needs orderbook history too

Run the recorder for 3-4 weeks, then we add Phase 2.

---

## Honest expectations

After the hard filters most leaderboard wallets fail. A typical run pulling
800 candidates may produce **0 to 3 A-tier wallets**. That's the system
working — most "smart money" on Polymarket is one of:
- market makers (hedge_ratio kills them)
- single lucky bet (profit_concentration kills them)
- 0.90+ late-arrival buyers (entry_090_plus_ratio kills them)
- inactive (last-30d filter kills them)

If `wallet_scores.csv` has zero A-tier wallets, that is the *correct* answer
under these filters — don't loosen them just to find someone to follow.
Loosen `WAR_LB_TAKE` or rerun with `WAR_DISCOVER_WINNERS=1` instead, to
expand the candidate pool.
