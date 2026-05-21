"""
Polymarket REST client (read-only for paper mode, extensible to live).

We only use `requests`. No heavy SDK. Enough for:
  * list active events + markets (Gamma)
  * fetch L2 orderbook for a token_id (CLOB)
  * (later) sign + post orders (CLOB) -- stubs only, disabled under paper_mode

Docs referenced:
  Gamma:  https://docs.polymarket.com/#gamma
  CLOB:   https://docs.polymarket.com/#clob
"""
from __future__ import annotations
import json
import time
import logging
from typing import Any
import urllib.parse
import urllib.request
import urllib.error

log = logging.getLogger("poly_client")


class PolyClient:
    def __init__(self, gamma_url: str, clob_url: str, timeout: float = 10.0):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url  = clob_url.rstrip("/")
        self.timeout   = timeout

    # --- low-level
    def _get(self, url: str, params: dict | None = None) -> Any:
        if params:
            url = url + "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers={"User-Agent": "poly-arb/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            log.warning("HTTP %s on %s: %s", e.code, url, e.read()[:200])
            return None
        except Exception as e:
            log.warning("GET failed %s: %s", url, e)
            return None

    # --- Gamma (events + metadata)
    def list_active_events(self, limit: int = 500, offset: int = 0) -> list[dict]:
        """Active, not-closed events. Each event has a list of child markets."""
        out = self._get(
            f"{self.gamma_url}/events",
            {"active": "true", "closed": "false", "limit": limit, "offset": offset},
        )
        return out or []

    def list_all_active_events(self, page_size: int = 500, hard_cap: int = 5000) -> list[dict]:
        all_ev: list[dict] = []
        off = 0
        while off < hard_cap:
            batch = self.list_active_events(limit=page_size, offset=off)
            if not batch:
                break
            all_ev += batch
            if len(batch) < page_size:
                break
            off += page_size
        return all_ev

    # --- CLOB (orderbook)
    def get_book(self, token_id: str) -> dict | None:
        """Return { 'bids':[{'price','size'},...], 'asks':[...] } for one binary outcome token."""
        return self._get(f"{self.clob_url}/book", {"token_id": token_id})

    def get_books_batch(self, token_ids: list[str]) -> list[dict]:
        """POST /books for multiple tokens at once (avoids rate limits)."""
        import urllib.request, json as _j
        url = f"{self.clob_url}/books"
        payload = _j.dumps([{"token_id": t} for t in token_ids]).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "poly-arb/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return _j.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            log.warning("books batch failed: %s", e)
            return []

    def get_midprice(self, token_id: str) -> float | None:
        r = self._get(f"{self.clob_url}/midpoint", {"token_id": token_id})
        if r and "mid" in r:
            try: return float(r["mid"])
            except: return None
        return None
