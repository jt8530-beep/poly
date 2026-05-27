"""
Cross-cutting utilities: logging, csv I/O, time, light retries.

Stdlib only.
"""
from __future__ import annotations
import csv
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def setup_logging(level: str = "INFO", name: str = "war") -> logging.Logger:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_ts(v: Any) -> float | None:
    """Parse a Polymarket-ish timestamp (unix seconds OR iso) -> unix seconds."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    # try int seconds / millis
    try:
        n = float(s)
        return n / 1000.0 if n > 1e12 else n
    except ValueError:
        pass
    # try iso
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None

def days_between(a: float, b: float) -> float:
    return abs(b - a) / 86400.0


# ---------------------------------------------------------------------------
# csv
# ---------------------------------------------------------------------------
def write_csv(path: str | Path, rows: Iterable[dict], fieldnames: list[str]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)

def append_csv(path: str | Path, rows: Iterable[dict], fieldnames: list[str]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists() or p.stat().st_size == 0
    n = 0
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)
            n += 1
    return n

def read_csv(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# tiny retry
# ---------------------------------------------------------------------------
def retry(fn, *, retries: int = 3, base_sleep: float = 0.5, log: logging.Logger | None = None):
    """Call fn() with exponential backoff. fn returns falsy -> retry."""
    last = None
    for i in range(retries):
        try:
            r = fn()
            if r not in (None, [], {}, False):
                return r
            last = r
        except Exception as e:                                    # noqa: BLE001
            last = e
            if log:
                log.warning("retry %d/%d: %s", i + 1, retries, e)
        time.sleep(base_sleep * (2 ** i))
    return last


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------
def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

def addr_lower(a: str | None) -> str:
    return (a or "").strip().lower()
