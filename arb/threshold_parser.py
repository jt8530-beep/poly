"""
Robust threshold extractor for ladder market questions.

Motivation
----------
v0 used a single regex that grabbed the first number in the question.
Counter-examples that broke it:

    "Will BTC be above $100,000 by Dec 31, 2026?"
        -> could match "31" (day of month) or "2026" (year)
    "Russia captures Kostyantynivka by July 31"
        -> grabbed "31" instead of being a time-only ladder
    "Fed rate cut of 25 bps in July?"
        -> grabbed "25" but "bps" is a unit we might want

v1 rules:
  1. Explicitly ignore years (1900..2099) and day-of-month numbers that
     immediately follow a month name.
  2. Prefer anchored tokens in this priority order:
       a) '$<num>'   currency
       b) '<num>%'   percent
       c) '>= <num>' / 'above <num>' / 'below <num>' / 'reach <num>'
       d) '<num>bps' / '<num> bp' / '<num> basis points'
       e) bare numbers only if question has no date-only hint
  3. Scale suffixes: k, m, b, thousand, million, billion.
  4. Return None if no clean candidate found.

API:  extract_threshold(question: str) -> float | None
"""
from __future__ import annotations
import re

# --- helpers -----------------------------------------------------------

_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# strip "Dec 31, 2026", "December 31", "on July 31st" so dates don't become thresholds
_DATE_STRIP_RX = re.compile(
    rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?",
    re.I,
)

# also strip bare 4-digit years in range 1900-2099
_YEAR_STRIP_RX = re.compile(r"\b(?:19|20)\d{2}\b")

_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
          "b": 1_000_000_000, "billion": 1_000_000_000,
          "t": 1_000_000_000_000, "trillion": 1_000_000_000_000}


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace("_", ""))
    except Exception:
        return None


def _scale(suf: str | None) -> float:
    if not suf:
        return 1.0
    s = suf.strip().lower()
    return _SCALE.get(s, 1.0)


# --- candidate regexes (priority-ordered) ------------------------------

# $100k / $100,000 / $1.5m  -- suffix MUST be at a word boundary so "by" / "this" don't hijack 'b'/'t'
_CURRENCY_RX = re.compile(
    r"\$\s*(?P<num>\d{1,3}(?:[,\d]*)(?:\.\d+)?)(?:\s*(?P<suf>k|m|b|t|thousand|million|billion|trillion)\b)?",
    re.I,
)

# 50% / 5.25%
_PERCENT_RX = re.compile(r"(?P<num>\d{1,3}(?:\.\d+)?)\s*%")

# 25bps / 25 bp / 25 basis points
_BPS_RX = re.compile(r"(?P<num>\d{1,4}(?:\.\d+)?)\s*(?:bps|bp|basis\s*points?)", re.I)

# "above 100k" / "below 50000" / "reach 100000" / ">= 100k" / "over 100k"
_ABOVE_BELOW_RX = re.compile(
    r"(?:above|below|over|under|greater\s+than|less\s+than|reach(?:es|ed)?|hit(?:s|)?|fall\s+below|>\s*=?|>=|<\s*=?|<=)\s*"
    r"\$?\s*(?P<num>\d{1,3}(?:[,\d]*)(?:\.\d+)?)(?:\s*(?P<suf>k|m|b|t|thousand|million|billion|trillion)\b)?",
    re.I,
)

# bare number with scale - fallback only (scale required, word-bounded)
_BARE_SCALED_RX = re.compile(
    r"\b(?P<num>\d{1,3}(?:[,\d]*)(?:\.\d+)?)\s*(?P<suf>k|m|b|t|thousand|million|billion|trillion)\b",
    re.I,
)

# last-resort bare integer >= some minimum (to avoid month days)
_BARE_INT_RX = re.compile(r"\b(?P<num>\d{3,}(?:[,\d]*)(?:\.\d+)?)\b")


def _clean(q: str) -> str:
    """Remove date tokens so they can't pollute the match."""
    s = _DATE_STRIP_RX.sub(" ", q)
    s = _YEAR_STRIP_RX.sub(" ", s)
    return s


def extract_threshold(question: str) -> float | None:
    """Return a numeric threshold, or None.

    Pipeline: strip dates/years -> priority regexes -> first hit wins.
    """
    if not question:
        return None
    cleaned = _clean(question)

    for rx in (_CURRENCY_RX, _PERCENT_RX, _BPS_RX, _ABOVE_BELOW_RX, _BARE_SCALED_RX):
        m = rx.search(cleaned)
        if not m:
            continue
        n = _num(m.group("num"))
        if n is None:
            continue
        suf = m.groupdict().get("suf") if "suf" in m.groupdict() else None
        return n * _scale(suf)

    # last resort: a standalone integer >= 100 (avoid month/day)
    m = _BARE_INT_RX.search(cleaned)
    if m:
        n = _num(m.group("num"))
        if n is not None and n >= 100:
            return n
    return None
