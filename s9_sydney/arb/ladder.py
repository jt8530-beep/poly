"""
Strategy C: Ladder monotonicity arbitrage on Polymarket.

v1 changes vs v0 (addressing the code review):
  * Threshold extraction delegated to threshold_parser (dates/years excluded).
  * Both legs use the SAME share count `q` (was: different sizes per leg).
  * Execution plan uses EXECUTABLE action pairs (BUY NO + BUY YES)
    rather than assuming we can naked-sell YES.
  * Signal now carries per-leg (action, token_outcome, price, size_shares, notional_usd)
    so the engine and ledger see the same numbers.

Background
----------
On Polymarket, many events are "ladders": N binary markets over ordered thresholds of
the same underlying quantity (time or numeric). By no-arbitrage:

  * time ladder:      P(YES by t1) <= P(YES by t2)   if t1 <= t2
  * threshold ladder: P(YES >= x1) >= P(YES >= x2)   if x1 <= x2

A violation opens a risk-free trade using TWO BUYS:

  time ladder, bid(YES_early) > ask(YES_late):
      1 YES token on Polymarket is minted from 1 USDC against 1 NO. So
      buying 1 NO_early + 1 YES_late gives guaranteed payoff >= $1 in every
      outcome where early happens (which forces late), AND gives $1+ when
      late happens but early doesn't -- a free lottery on top of a locked
      positive edge.

  threshold ladder, bid(YES_high) > ask(YES_low):
      buy NO_high + buy YES_low (same logic, reversed direction)

  "Locked profit per share" = 1 - (cost_leg_a + cost_leg_b)
     where cost_leg_a = ask of the NO side
           cost_leg_b = ask of the YES side
     This must be > 0 after fees to be a signal.

Execution is NOT in this file; see executor.py. This module is pure compute.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .threshold_parser import extract_threshold

log = logging.getLogger("ladder")


# ----------------------------- Data classes -----------------------------

@dataclass
class MarketSide:
    """One binary market snapshot with both YES and NO books."""
    market_id: str
    question: str
    slug: str
    token_id_yes: str | None
    token_id_no:  str | None
    best_bid_yes: float | None = None
    best_ask_yes: float | None = None
    best_bid_no:  float | None = None
    best_ask_no:  float | None = None
    bid_size_yes: float | None = None
    ask_size_yes: float | None = None
    bid_size_no:  float | None = None
    ask_size_no:  float | None = None
    order_key: Any = None        # float (threshold) or datetime (time)
    end_date: str | None = None
    neg_risk: bool = False
    neg_risk_other: bool = False
    group_item_title: str | None = None


@dataclass
class Leg:
    """A single order in a pair. All fields chosen so executor can act on it directly."""
    market_id: str
    question: str
    token_id: str                # CLOB token_id of the outcome we BUY
    token_outcome: str           # "YES" or "NO"
    side: str                    # always "BUY" for v1 (we don't naked-sell)
    price: float                 # best ask we'll lift
    size_shares: float           # number of outcome tokens
    notional_usd: float          # size_shares * price (cost to us)
    action_str: str              # human-readable


@dataclass
class LadderSignal:
    event_slug: str
    event_title: str
    kind: str                    # "time" or "threshold"
    leg_a: Leg
    leg_b: Leg
    edge_bps: int                # (1 - cost_total) * 10_000
    expected_profit_usd: float   # (1 - cost_total) * q
    max_size_usd: float          # total committed capital (cost_total * q)
    q_shares: float              # the SHARED share count both legs use
    notes: str = ""

    # Back-compat shims for main.py/ledger.py: they still reference leg_a_market etc.
    @property
    def leg_a_market(self) -> MarketSide:   # pragma: no cover
        return _back_compat_market(self.leg_a)

    @property
    def leg_b_market(self) -> MarketSide:   # pragma: no cover
        return _back_compat_market(self.leg_b)

    @property
    def leg_a_action(self) -> str: return self.leg_a.action_str
    @property
    def leg_b_action(self) -> str: return self.leg_b.action_str


def _back_compat_market(leg: Leg) -> MarketSide:
    # The ledger only reads market_id and .question from these shims; fill just enough.
    return MarketSide(
        market_id=leg.market_id, question=leg.question, slug="",
        token_id_yes=None, token_id_no=None,
    )


# ----------------------------- Parsing helpers -----------------------------

def _parse_end_date(s: str | None) -> datetime | None:
    if not s:
        return None


_RANGE_BUCKET_RX = re.compile(r"\bbetween\b|\bfrom\b.+\bto\b", re.I)
_PROB_DECREASES_WITH_THRESHOLD_RX = re.compile(
    r"\b(?:above|over|greater\s+than|more\s+than|at\s+least|reach(?:es|ed)?|exceed(?:s|ed)?)\b|>=|>",
    re.I,
)
_PROB_INCREASES_WITH_THRESHOLD_RX = re.compile(
    r"\b(?:below|under|less\s+than|at\s+most|fall(?:s|en)?\s+(?:below|to)|dip(?:s|ped)?\s+to|drop(?:s|ped)?\s+to)\b|<=|<",
    re.I,
)


def _threshold_direction(question: str) -> str | None:
    """Return how YES probability moves as the numeric threshold increases.

    "BTC above 100k/120k" decreases as the threshold rises.
    "BTC below 80k/60k" increases as the threshold rises.
    Range buckets like "between 900B and 1T" are not cumulative ladders.
    """
    if not question or _RANGE_BUCKET_RX.search(question):
        return None
    lowered = question.lower()
    if re.search(r"\bor\s+(?:lower|less|below)\b", lowered):
        return "increases"
    if re.search(r"\bor\s+(?:higher|more|above|greater)\b", lowered):
        return "decreases"
    if "(low)" in lowered or " hit low " in lowered or " hit (low) " in lowered:
        return "increases"
    if "(high)" in lowered or " hit high " in lowered or " hit (high) " in lowered:
        return "decreases"
    decreases = bool(_PROB_DECREASES_WITH_THRESHOLD_RX.search(question))
    increases = bool(_PROB_INCREASES_WITH_THRESHOLD_RX.search(question))
    if decreases == increases:
        return None
    return "decreases" if decreases else "increases"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_outcome_tokens(market: dict) -> tuple[str | None, str | None]:
    """Return (yes_token_id, no_token_id). Handles multiple Gamma response shapes."""
    import json as _j
    toks = market.get("clobTokenIds")
    if isinstance(toks, str):
        try: toks = _j.loads(toks)
        except Exception: toks = None
    if isinstance(toks, list) and len(toks) >= 2:
        return str(toks[0]), str(toks[1])
    tlist = market.get("tokens")
    if isinstance(tlist, list):
        y = n = None
        for t in tlist:
            o = (t.get("outcome") or "").lower()
            tid = t.get("token_id") or t.get("id")
            if o.startswith("y"): y = tid
            elif o.startswith("n"): n = tid
        return (str(y) if y else None, str(n) if n else None)
    return None, None


def build_markets_for_event(event: dict) -> list[MarketSide]:
    out: list[MarketSide] = []
    for mk in event.get("markets") or []:
        if mk.get("closed") or mk.get("archived") or not mk.get("active", True):
            continue
        y, n = _parse_outcome_tokens(mk)
        if not y or not n:
            continue
        out.append(MarketSide(
            market_id   = str(mk.get("id", "")),
            question    = mk.get("question", ""),
            slug        = mk.get("slug", ""),
            token_id_yes= y,
            token_id_no = n,
            end_date    = mk.get("endDate"),
            neg_risk    = bool(mk.get("negRisk")),
            neg_risk_other = bool(mk.get("negRiskOther")),
            group_item_title = mk.get("groupItemTitle"),
        ))
    return out


def classify_and_sort(markets: list[MarketSide]) -> tuple[str, list[MarketSide]]:
    """Return ('time'|'threshold'|'none', markets_sorted_ascending_by_key)."""
    dates = [_parse_end_date(m.end_date) for m in markets]
    if all(dates) and len({d.date() for d in dates}) == len(markets) and len(markets) >= 3:
        paired = sorted(zip(dates, markets), key=lambda x: x[0])
        for d, m in paired: m.order_key = d
        return "time", [m for _, m in paired]

    parsed = [(extract_threshold(m.question), _threshold_direction(m.question), m) for m in markets]
    good = [(t, direction, m) for t, direction, m in parsed if t is not None and direction is not None]
    directions = {direction for _, direction, _ in good}
    if len(good) >= 3 and len({t for t, _, _ in good}) >= 3 and len(directions) == 1:
        direction = next(iter(directions))
        # detect_violations assumes sorted order has non-increasing YES probability.
        # For "above/reach" markets that is ascending threshold; for "below/fall"
        # markets that is descending threshold.
        reverse = direction == "increases"
        good.sort(key=lambda x: x[0], reverse=reverse)
        for t, _, m in good:
            m.order_key = t
        return "threshold", [m for _, _, m in good]

    return "none", markets


# ----------------------------- Orderbook helpers -----------------------------

def _top_of_book(book: dict) -> tuple[float | None, float | None, float | None, float | None]:
    """(best_bid, best_bid_size, best_ask, best_ask_size)."""
    if not book:
        return None, None, None, None
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def _level(row: dict) -> tuple[float, float] | None:
        try:
            return float(row.get("price")), float(row.get("size"))
        except Exception:
            return None

    bid_levels = [level for row in bids if (level := _level(row)) is not None]
    ask_levels = [level for row in asks if (level := _level(row)) is not None]
    bb, bbs = max(bid_levels, key=lambda level: level[0]) if bid_levels else (None, None)
    ba, bas = min(ask_levels, key=lambda level: level[0]) if ask_levels else (None, None)
    return bb, bbs, ba, bas


def populate_books(client, markets: list[MarketSide]) -> None:
    """Fill best_bid/ask/size fields by fetching YES and NO books."""
    token_ids: list[str] = []
    for m in markets:
        if m.token_id_yes: token_ids.append(m.token_id_yes)
        if m.token_id_no:  token_ids.append(m.token_id_no)
    if not token_ids:
        return
    books = client.get_books_batch(token_ids)
    by_tok: dict[str, dict] = {}
    for b in books or []:
        k = str(b.get("asset_id") or b.get("token_id") or "")
        if k: by_tok[k] = b
    for m in markets:
        by = by_tok.get(m.token_id_yes or "")
        bn = by_tok.get(m.token_id_no or "")
        bb, bbs, ba, bas = _top_of_book(by) if by else (None, None, None, None)
        m.best_bid_yes, m.bid_size_yes, m.best_ask_yes, m.ask_size_yes = bb, bbs, ba, bas
        bb, bbs, ba, bas = _top_of_book(bn) if bn else (None, None, None, None)
        m.best_bid_no, m.bid_size_no, m.best_ask_no, m.ask_size_no = bb, bbs, ba, bas


# ----------------------------- Signal detection -----------------------------

def _mk_leg(m: MarketSide, outcome: str, price: float, ask_size: float | None,
            q_shares: float, action: str) -> Leg:
    token_id = m.token_id_yes if outcome == "YES" else m.token_id_no
    return Leg(
        market_id = m.market_id,
        question  = m.question,
        token_id  = token_id or "",
        token_outcome = outcome,
        side = "BUY",
        price = float(price),
        size_shares = float(q_shares),
        notional_usd = float(q_shares * price),
        action_str = action,
    )


def detect_violations(
    event: dict,
    markets_sorted: list[MarketSide],
    kind: str,
    min_edge_bps: int = 200,
    min_notional_usd: float = 20.0,
    max_notional_per_leg: float = 30.0,
) -> list[LadderSignal]:
    """
    v1 pairs two BUY orders whose combined payoff is >= $1 per share in every state:

      Time ladder (expect P(a) <= P(b)): if bid(YES_a) > ask(YES_b) is violated,
        equivalent executable edge exists when:
           cost = ask(NO_a) + ask(YES_b)   <   1
        Then in every world we recover >= $1:
           world "a" happens  -> also "b" happens (time ladder).
                              -> NO_a = 0, YES_b = 1, payoff = 1
           world "a" not, "b" happens     -> NO_a = 1, YES_b = 1, payoff = 2
           world neither happens          -> NO_a = 1, YES_b = 0, payoff = 1
        Edge per share = 1 - cost.

      Threshold ladder (expect P(a) >= P(b)): reversed, buy NO_b + buy YES_a.

    Both shares counted as the SAME q (the binding depth).
    """
    if kind not in ("time", "threshold") or len(markets_sorted) < 2:
        return []

    signals: list[LadderSignal] = []
    event_slug  = event.get("slug", "")
    event_title = event.get("title", "")

    for i in range(len(markets_sorted) - 1):
        a = markets_sorted[i]
        b = markets_sorted[i + 1]
        # By sort order: P_a <= P_b for time, P_a >= P_b for threshold
        if kind == "time":
            # executable pair: buy NO_a + buy YES_b
            cost_a = a.best_ask_no
            cost_b = b.best_ask_yes
            depth_a = a.ask_size_no
            depth_b = b.ask_size_yes
            outcome_a, outcome_b = "NO", "YES"
            market_a, market_b = a, b
            reason = "P(YES_a) <= P(YES_b) violated; capture via NO_a+YES_b"
        else:  # threshold
            # executable pair: buy NO_b + buy YES_a
            cost_a = b.best_ask_no
            cost_b = a.best_ask_yes
            depth_a = b.ask_size_no
            depth_b = a.ask_size_yes
            outcome_a, outcome_b = "NO", "YES"
            market_a, market_b = b, a
            reason = "P(YES_a) >= P(YES_b) violated; capture via NO_b+YES_a"

        if cost_a is None or cost_b is None:
            continue
        total_cost = cost_a + cost_b
        if total_cost >= 1.0:
            continue     # no free $1 in the combined payoff
        edge_per_share = 1.0 - total_cost
        edge_bps = int(edge_per_share * 10_000)
        if edge_bps < min_edge_bps:
            continue

        # q = shared share count, bounded by both asks' depth and per-leg notional cap.
        # ask_size on Polymarket CLOB is quoted in outcome-token shares (== USDC at fill-in-full).
        depth_shares = min(depth_a or 0.0, depth_b or 0.0)
        if depth_shares <= 0:
            continue
        # per-leg notional cap translates to a shares cap via price
        shares_cap_from_notional = min(
            max_notional_per_leg / max(cost_a, 1e-6),
            max_notional_per_leg / max(cost_b, 1e-6),
        )
        q = min(depth_shares, shares_cap_from_notional)

        total_notional = q * total_cost     # capital we deploy
        if total_notional < min_notional_usd:
            continue

        action_a = f"BUY {outcome_a} @ {cost_a:.4f}  (size {q:.2f} sh, ${q*cost_a:.2f})"
        action_b = f"BUY {outcome_b} @ {cost_b:.4f}  (size {q:.2f} sh, ${q*cost_b:.2f})"

        leg_a = _mk_leg(market_a, outcome_a, cost_a, depth_a, q, action_a)
        leg_b = _mk_leg(market_b, outcome_b, cost_b, depth_b, q, action_b)

        signals.append(LadderSignal(
            event_slug = event_slug,
            event_title = event_title,
            kind = kind,
            leg_a = leg_a,
            leg_b = leg_b,
            edge_bps = edge_bps,
            expected_profit_usd = round(edge_per_share * q, 4),
            max_size_usd = round(total_notional, 2),
            q_shares = round(q, 4),
            notes = f"pair idx {i}<->{i+1}: {reason}",
        ))
    return signals


def scan_event(client, event: dict, min_edge_bps: int,
               min_notional_usd: float,
               max_notional_per_leg: float) -> list[LadderSignal]:
    ms = build_markets_for_event(event)
    if len(ms) < 3: return []
    kind, sorted_ms = classify_and_sort(ms)
    if kind == "none": return []
    populate_books(client, sorted_ms)
    return detect_violations(
        event, sorted_ms, kind,
        min_edge_bps=min_edge_bps,
        min_notional_usd=min_notional_usd,
        max_notional_per_leg=max_notional_per_leg,
    )
