# Postmortem — 2026-05-26 PLTR LP Loss

**Loss**: $5.90 → ~$2.65 (live MtM at write time, ~−55%) on the first manual
deploy of the LP market-maker scanner. Settles 2026-05-31; final realized
PnL TBD.

**Root cause**: Scanner ranked equity-derivative markets at the top of
"sparse competition" tier without applying any qualitative filter for
inventory volatility risk. Manual deploy followed scanner output literally.

This file documents what was wrong, what it cost, and what changed in the
code (commit history references `feat/lp-market-maker` branch).

## What happened

1. User asked to validate gucky-gu45-style LP strategy: "esports dead zone,
   挂单占位, $4 → $26 reward".
2. Scanner found `Will PLTR (HIGH) $150 in May` at "sparse" rank (wide
   spread, low capital, 5.6 days to settle, $41 daily reward pool).
3. Scanner suggested PLTR + EWY (also equity ETF) as a "diversified pair".
4. User manually placed `LIMIT BUY @ 11.8¢ × 50` on PLTR. Order filled
   immediately (= took inventory at current ask, $5.90 cost).
5. Before user placed the corresponding `LIMIT SELL`, PLTR equity dropped
   in pre-market US trading. The Polymarket prediction price for "$150
   strike in May" dropped from 16% probability to ~5%.
6. Inventory MtM: $5.90 → $2.65, unrealized loss −$3.25 (−55%).
7. LP rewards earned: $0 (no SELL order on book during the move).

## Why scanner was wrong

The scanner found three structurally identical markets at the top of
"sparse" tier:
- AAPL HIGH $320 in May
- PLTR HIGH $150 in May
- EWY HIGH $204 in May

All three are equity-derivative prediction markets. Their **wide
spreads are not sparse competition** — they are **risk premiums**:
professional market makers won't tighten the book because the underlying
moves 24/7 (futures, ADRs, news flow) and any tight spread will be
adverse-selected. Retail can't tighten because they fear the same.

Contrast with the gucky-gu45 setup: an esports mid-game break has wide
spreads because **nobody is paying attention** (during the literal
60-minute break) — but inventory price won't move because the underlying
event is paused. Wide spread → opportunity, not risk.

Two different mechanisms generate "wide spread"; v0 scanner conflated
them. The qualitative filter was missing.

## Specific scanner mistakes (v0)

| # | Mistake | What v2 changed |
|---|---------|-----------------|
| 1 | No tag-based filtering | Added `LP_BLACKLIST_TAGS` (Finance/Stocks/Equity/Macro/Politics) and `LP_WHITELIST_TAGS` (Esports/Games/Sports/Weather) |
| 2 | "Diversified pair" recommendation included two equity derivatives | Whitelist + blacklist make this physically impossible going forward |
| 3 | Capital cap of $50/market excluded the actual best fit (DOTA 2 BO1 with $107 daily and 5.7h horizon, capital $193) — high capital ≠ high risk for stable-inventory markets | Documented in README: "high capital with whitelist tag is fine; the cap was a v0 hedge against equity markets" |
| 4 | No time-of-day awareness — recommended "deploy now" at UTC 12:00 (= US pre-market for equities) | Documented; v3 should add `time_of_day_appropriate_for_class` flag per market class |
| 5 | Suggested SELL price was set but the user's manual deploy missed step 3 — system gave no help recovering | Phase 1 (automated executor) would have placed all three orders atomically. Until built, README emphasizes the three-step deploy must be one continuous action |

## Things that worked

* Hard cap $50/market saved this from being a bigger loss.
* "Hold to settle" recommendation (option A) is consistent with EV math:
  current 5% probability × $50 payout = $2.50 expected, vs $1.30 from
  selling at bid now. Holding has slightly better EV than realizing
  the loss now.
* Tracker (02_track_my_orders.py) correctly identified the position,
  filtered out btc-updown-* quant noise, and surfaced the cost-basis +
  MtM gap. Cost basis pulled from activity feed when /trades didn't
  paginate the recent buy.

## New rules (encoded in v2)

1. **Tag whitelist required**: at least one of `Esports`, `Games`,
   `Sports`, `Weather` (or whatever the user adds via `LP_WHITELIST_TAGS`).
2. **Tag blacklist hard-rejects** any of: `Finance`, `Stocks`, `Equity`,
   `ETF[s]`, `Macro`, `Crypto Prices`, `Hit Price`, `Politics`,
   `Election[s]`, `FX`, `Forex`, `Bonds`, `Commodities`, `Energy`,
   `Interest Rates`. Even if reward pool is huge.
3. **Diversification check** (planned): when scanner outputs N
   recommendations, mark any pair with shared tags or shared underlying
   asset symbol as "correlated, not independent".
4. **Time-of-day awareness** (planned): equity-class markets should only
   be recommended during their dead-zone (US after-hours + Asia close,
   roughly UTC 21:00–08:00 EDT off-hours). v0 ignored this entirely.
5. **Inventory volatility filter** (planned): track a market's mid-price
   stdev over the past 24h via the /price-history endpoint; reject any
   where stdev > 0.05 regardless of class.

## Cost of lesson

* `−$3.25` unrealized at write time, max `−$5.90` at settle if PLTR
  fails to hit $150 by 5/31 (~95% likely outcome at current 5%
  probability).
* For comparison, building scanner v2 + tracker took ~2 hours of
  engineering. The $5.90 paid for two filters that v0 should have
  shipped with from day one.

## What's still open

1. Phase 1 (automated CLOB write client + EIP-712 signing) — deferred
   to next iteration. Tonight's manual flow surfaced the 3-step deploy
   atomicity problem; Phase 1 will solve it.
2. Polymarket reward formula fitting — heuristic 70%/15% capture is a
   guess. Calibrate against actual reward credits over the next 7-14
   days of paper data.
3. Inventory price-history endpoint — confirm `/prices-history` exists
   on gamma or CLOB; use it for the volatility filter.

## Never again

* No equity derivatives in LP scanner output.
* No "diversified pair" claim without explicit cross-asset uncorrelated
  check.
* No deployment recommendation during US market hours for any
  equity-adjacent market.
* No three-step manual deploy without Phase 1 atomic executor.



---

## Update 2026-05-26 16:49 UTC: PLTR closed, v2 not strict enough

PLTR `LIMIT SELL @ 7.5¢ × 50` filled at 16:49:20 UTC.

Realized PnL: 49.99 × 0.075 = $3.75 ;  cost $5.90 ;  **realized loss = −$2.15**.
Better than worst-case −$5.90 hold-to-settle. The SELL order earned the
LP-time-on-book between 14:30 and 16:49 (≈2h20m) — whether reward credit
shows up in `/activity` is TBD (no REWARD entries yet, may need direct
`/rewards` endpoint research).

WorkBuddy then ran scanner v2 with `LP_WHITELIST_TAGS=Esports,Games,Sports,Weather`
and discovered v2 is still leaky: top-19 results included MLB / NHL /
Tennis / Soccer markets, because **traditional sports markets are tagged
with both `Sports` AND `Games`**, and we whitelisted both.

### v2.1 (this update)

* Whitelist tightened to `Esports,Weather` only. `Games` removed (catches
  trad sports), `Sports` removed (catches everything).
* Blacklist extended with traditional sports leagues:
  `MLB, NHL, NBA, NFL, Soccer, Tennis, Cricket, Football, Baseball, Hockey,
  Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Champions League`.

### Why this matters (per user question)

> 体育中场休息和电竞中场逻辑一样吗？

**No.** Esports digital game state can be *fully paused* — no new info
flows during the break. Traditional sports half-time is dense
public re-evaluation (first-half score, momentum, key player stats,
injuries) — sharp money repositions, prediction price moves. The "dead
zone" assumption only holds for esports.

### Open question for v3

Some valid plays may live in `Cricket innings break` or `Tennis between
sets` (lower volume, less re-evaluation than NBA/NFL halftime). v2.1
blacklists them defensively; if real PnL data later shows they're fine,
loosen selectively. For now: tight is right.

### Score so far

| | $ |
|---|---|
| Total LP capital deployed | $5.90 |
| Realized loss | $2.15 |
| LP rewards earned | $0 (or unknown — endpoint research pending) |
| Net | **−$2.15** |
| Lessons banked | 7 (PLTR root cause + v2 leak + sports vs esports + reward endpoint gap + 4 from v0 postmortem) |
