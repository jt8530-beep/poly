"""
Strategy C: Ladder monotonicity arbitrage on Polymarket.

Background
----------
On Polymarket, many events come as a "ladder": N binary markets on ordered thresholds of
the same underlying quantity. Examples:

    Russia captures Kostyantynivka by   May 31   (YES price 0.08)
                                        Jun 30   (YES price 0.20)
                                        Jul 31   (YES price 0.40)
                                        Aug 31   (YES price 0.55)
                                        ...

The event "YES by Aug 31" is a SUPERSET of "YES by May 31" (time-ordered ladder), so by
no-arbitrage:

    price(YES, May 31)  <=  price(YES, Jun 30)  <=  price(YES, Jul 31)   ...
    price(NO,  May 31)  >=  price(NO,  Jun 30)  >=  price(NO,  Jul 31)   ...

Same for price-threshold ladders ("BTC >= 100k", "BTC >= 110k" at a fixed date):

    price(YES, >= 100k)  >=  price(YES, >= 110k)  >=  price(YES, >= 120k)   ...

When the book violates this ordering (by > threshold_bps after fees) we can trade
it:

  * time ladder violation:  p(YES near) > p(YES far)
       => sell YES_near, buy YES_far     (equivalent: buy NO_near, sell NO_far)
  * threshold ladder violation:  p(YES low) < p(YES high)
       => buy YES_low, sell YES_high    OR buy NO_high, sell NO_low

Payoff is bounded and, under Polymarket's binary resolution rules, nonnegative in every
state of the world -- i.e. risk-free IF we control both fills.

This module:
  1. Detects ladder-like events from Gamma metadata.
  2. Parses ordering per event (time-ordered or threshold-ordered).
  3. Scans for violations using top-of-book from CLOB.
  4. Emits a LadderSignal with the pair(s) and expected edge.

Execution is NOT in this file; see executor.py. This module is pure compute.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("ladder")


# ----------------------------- Data classes -----------------------------

@dataclass
class MarketSide:
    """One binary market snapshot (YES-side token)."""
    market_id: str
    question: str
    slug: str
    token_id_yes: str | None        # CLOB token for YES
    token_id_no:  str | None        # CLOB token for NO
    best_bid_yes: float | None = None
    best_ask_yes: float | None = None
    best_bid_no:  float | None = None
    best_ask_no:  float | None = None
    bid_size_yes: float | None = None
    ask_size_yes: float | None = None
    bid_size_no:  float | None = None
    ask_size_no:  float | None = None
    # ordering key (float or date)
    order_key: Any = None
    end_date: str | None = None


@dataclass
class LadderSignal:
    event_slug: str
    event_title: str
    kind: str                       # "time" or "threshold"
    # Two legs. Each leg includes an action.
    leg_a_market: MarketSide
    leg_b_market: MarketSide
    leg_a_action: str              # e.g. "BUY YES_A @ ask"
    leg_b_action: str              # e.g. "SELL YES_B @ bid"
    edge_bps: int
    expected_profit_usd: float
    max_size_usd: float
    notes: str = ""


# ----------------------------- Parsing helpers -----------------------------

_DATE_RX = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?\b", re.I)

def _parse_end_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # e.g. "2026-06-30T00:00:00Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


_NUM_RX = re.compile(r"(?P<num>\d{1,3}(?:[,\d]*)(?:\.\d+)?)(?P<suf>k|m|b)?", re.I)

def _extract_threshold(question: str) -> float | None:
    """Best-effort: extract a primary numeric threshold from a market question."""
    # prefer patterns like 'above $100k', '>=  500', 'reach 100,000', etc.
    q = question.lower()
    # very simple: first number found
    m = _NUM_RX.search(q)
    if not m:
        return None
    num = m.group("num").replace(",", "")
    try:
        v = float(num)
    except Exception:
        return None
    suf = (m.group("suf") or "").lower()
    if suf == "k": v *= 1_000
    elif suf == "m": v *= 1_000_000
    elif suf == "b": v *= 1_000_000_000
    return v


def _parse_outcome_tokens(market: dict) -> tuple[str | None, str | None]:
    """Polymarket market json carries a clobTokenIds or tokens list.

    We try a few shapes, since the Gamma response has mutated over time:
      * market["clobTokenIds"] = "[\"<yes>\",\"<no>\"]"
      * market["tokens"] = [{"outcome":"Yes","token_id":...},{"outcome":"No","token_id":...}]
    """
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
    """Turn a Gamma event dict into a list of MarketSide (no book yet)."""
    out: list[MarketSide] = []
    for mk in event.get("markets") or []:
        if mk.get("closed") or mk.get("archived") or not mk.get("active", True):
            continue
        y, n = _parse_outcome_tokens(mk)
        if y is None or n is None:
            continue
        ms = MarketSide(
            market_id   = str(mk.get("id", "")),
            question    = mk.get("question", ""),
            slug        = mk.get("slug", ""),
            token_id_yes= y,
            token_id_no = n,
            end_date    = mk.get("endDate"),
        )
        out.append(ms)
    return out


def classify_and_sort(markets: list[MarketSide]) -> tuple[str, list[MarketSide]]:
    """Return ('time'|'threshold'|'none', markets_sorted_ascending_by_key).

    Rules:
      - If all markets in the event have DIFFERENT endDates, it's a time ladder.
      - Else, try to extract numeric threshold from the question; if at least 3 differ,
        it's a threshold ladder.
    """
    dates = [_parse_end_date(m.end_date) for m in markets]
    if all(dates) and len({d.date() for d in dates}) == len(markets) and len(markets) >= 3:
        paired = sorted(zip(dates, markets), key=lambda x: x[0])
        for d, m in paired: m.order_key = d
        return "time", [m for _, m in paired]

    thrs = [_extract_threshold(m.question) for m in markets]
    if sum(1 for t in thrs if t is not None) >= 3:
        pairs = [(t if t is not None else -1e18, m) for t, m in zip(thrs, markets)]
        pairs = [p for p in pairs if p[0] > -1e17]
        pairs.sort(key=lambda x: x[0])
        if len({p[0] for p in pairs}) >= 3:
            for t, m in pairs: m.order_key = t
            return "threshold", [m for _, m in pairs]

    return "none", markets


# ----------------------------- Signal detection -----------------------------

def _top_of_book(book: dict) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (best_bid, best_bid_size, best_ask, best_ask_size).

    Polymarket CLOB /book returns { 'bids': [{'price':'...', 'size':'...'}], 'asks': [...] }.
    Bids descending; asks ascending. Some implementations reverse asks -- handle both.
    """
    if not book:
        return None, None, None, None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    def _f(x, k):
        try: return float(x.get(k))
        except: return None
    bb = _f(bids[0], "price") if bids else None
    bbs = _f(bids[0], "size")  if bids else None
    ba = _f(asks[0], "price") if asks else None
    bas = _f(asks[0], "size")  if asks else None
    # sanity: ask >= bid; if flipped, swap asks (some APIs return descending asks)
    if bb is not None and ba is not None and ba < bb and len(asks) > 1:
        last = _f(asks[-1], "price")
        if last is not None and last > bb:
            ba = last
            bas = _f(asks[-1], "size")
    return bb, bbs, ba, bas


def populate_books(client, markets: list[MarketSide]) -> None:
    """Fill best_bid/ask/size fields in-place by querying the CLOB."""
    token_ids = []
    for m in markets:
        if m.token_id_yes: token_ids.append(m.token_id_yes)
        if m.token_id_no:  token_ids.append(m.token_id_no)
    if not token_ids:
        return
    books = client.get_books_batch(token_ids)
    by_tok: dict[str, dict] = {}
    for b in books or []:
        # shape: { 'asset_id': '...', 'bids': [...], 'asks': [...] }
        k = str(b.get("asset_id") or b.get("token_id") or "")
        if k: by_tok[k] = b
    for m in markets:
        by = by_tok.get(m.token_id_yes or "")
        bn = by_tok.get(m.token_id_no or "")
        bb, bbs, ba, bas = _top_of_book(by) if by else (None, None, None, None)
        m.best_bid_yes, m.bid_size_yes, m.best_ask_yes, m.ask_size_yes = bb, bbs, ba, bas
        bb, bbs, ba, bas = _top_of_book(bn) if bn else (None, None, None, None)
        m.best_bid_no, m.bid_size_no, m.best_ask_no, m.ask_size_no = bb, bbs, ba, bas


def detect_violations(
    event: dict,
    markets_sorted: list[MarketSide],
    kind: str,
    min_edge_bps: int = 200,
    min_depth_usd: float = 20.0,
) -> list[LadderSignal]:
    """Scan all adjacent pairs (i, i+1) in the sorted ladder.

    For a YES ladder that should be non-decreasing (time ladder OR threshold-low-to-high
    of 'YES above X'), violation is:  ask(i+1) < bid(i)  => sell YES_i, buy YES_{i+1}.

    For a YES ladder that should be non-increasing (threshold ladder where increasing
    threshold means lower prob), violation is:  bid(i+1) > ask(i)  => sell YES_{i+1},
    buy YES_i.

    We detect both directions by always checking:  bid(big_p_side) > ask(small_p_side)
    with an explicit sign based on `kind`.

    For the ladder we assume probability ORDER follows the sort:
      kind == 'time'      : prob non-decreasing      (later endDate -> >= earlier)
      kind == 'threshold' : prob non-increasing      (higher threshold -> <= lower)
    """
    if kind not in ("time", "threshold") or len(markets_sorted) < 2:
        return []

    signals: list[LadderSignal] = []
    event_slug  = event.get("slug", "")
    event_title = event.get("title", "")

    for i in range(len(markets_sorted) - 1):
        a = markets_sorted[i]
        b = markets_sorted[i + 1]
        if kind == "time":
            # expect P(YES_a) <= P(YES_b). violation if bid(YES_a) > ask(YES_b).
            high, low = a, b      # the one that OUGHT to be lower in price = a
            # sell the high one, buy the low one
            high_bid = high.best_bid_yes
            low_ask  = low.best_ask_yes
            high_bid_size = high.bid_size_yes
            low_ask_size  = low.ask_size_yes
            action_high = f"SELL YES @ {high_bid}"
            action_low  = f"BUY  YES @ {low_ask}"
        else:  # threshold
            # expect P(YES_a) >= P(YES_b). violation if bid(YES_b) > ask(YES_a).
            high, low = b, a
            high_bid = high.best_bid_yes
            low_ask  = low.best_ask_yes
            high_bid_size = high.bid_size_yes
            low_ask_size  = low.ask_size_yes
            action_high = f"SELL YES @ {high_bid}"
            action_low  = f"BUY  YES @ {low_ask}"

        if high_bid is None or low_ask is None:
            continue
        raw_edge = high_bid - low_ask
        if raw_edge <= 0:
            continue
        edge_bps = int(raw_edge * 10_000)
        if edge_bps < min_edge_bps:
            continue
        # size-limited expected profit
        size_cap = min(high_bid_size or 0, low_ask_size or 0)
        # size on CLOB is typically in USDC notional; treat as USD here
        if size_cap < min_depth_usd / max(max(high_bid, low_ask), 1e-6):
            continue

        max_size_usd = min(size_cap * low_ask, size_cap * high_bid)
        expected_profit = raw_edge * size_cap

        signals.append(LadderSignal(
            event_slug = event_slug,
            event_title = event_title,
            kind = kind,
            leg_a_market = high,
            leg_b_market = low,
            leg_a_action = action_high,
            leg_b_action = action_low,
            edge_bps = edge_bps,
            expected_profit_usd = round(expected_profit, 4),
            max_size_usd = round(max_size_usd, 2),
            notes = f"adjacent pair idx {i}<->{i+1}",
        ))
    return signals


def scan_event(client, event: dict, min_edge_bps: int, min_depth_usd: float) -> list[LadderSignal]:
    ms = build_markets_for_event(event)
    if len(ms) < 3: return []
    kind, sorted_ms = classify_and_sort(ms)
    if kind == "none": return []
    populate_books(client, sorted_ms)
    return detect_violations(event, sorted_ms, kind, min_edge_bps, min_depth_usd)


def scan_all(client, events: list[dict], min_edge_bps: int, min_depth_usd: float) -> list[LadderSignal]:
    sigs: list[LadderSignal] = []
    for ev in events:
        try:
            sigs += scan_event(client, ev, min_edge_bps, min_depth_usd)
        except Exception as e:
            log.warning("scan_event failed for %s: %s", ev.get("slug","?"), e)
    return sigs
