"""
Minimal stdlib client for gamma /events. Same defensive HTTP pattern used
in overround_arb / wallet_alpha_radar.
"""
from __future__ import annotations
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import ApiConfig

log = logging.getLogger("lp.api")


class ApiClient:
    def __init__(self, cfg: ApiConfig):
        self.cfg = cfg

    def _request(self, url: str) -> Any:
        h = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        req = urllib.request.Request(url, headers=h)
        last_err: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as r:
                    raw = r.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    last_err = e
                    sleep_for = self.cfg.sleep_between * (2 ** attempt)
                    log.warning("HTTP %s (try %d) — sleep %.1fs", e.code, attempt + 1, sleep_for)
                    time.sleep(sleep_for)
                    continue
                log.warning("HTTP %s on %s", e.code, url)
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                time.sleep(self.cfg.sleep_between * (2 ** attempt))
            except Exception as e:                                # noqa: BLE001
                log.warning("err on %s: %s", url, e)
                return None
        log.error("giving up on %s: %s", url, last_err)
        return None

    def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True)
        out = self._request(url)
        time.sleep(self.cfg.sleep_between)
        return out

    def list_events_page(self, *, closed: bool = False, limit: int = 100,
                         offset: int = 0, order: str = "volume",
                         ascending: bool = False) -> list[dict]:
        resp = self._get(self.cfg.gamma_url, "/events", {
            "closed": "true" if closed else "false",
            "limit": limit, "offset": offset,
            "order": order,
            "ascending": "true" if ascending else "false",
        })
        return resp if isinstance(resp, list) else []

    def iter_events(self, *, page_size: int = 100, max_pages: int = 15) -> list[dict]:
        out: list[dict] = []
        for page in range(max_pages):
            batch = self.list_events_page(limit=page_size, offset=page * page_size)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out

    def book(self, token_id: str) -> dict | None:
        """Full orderbook for a token — used by tracker, not the scanner."""
        resp = self._get(self.cfg.clob_url, "/book", {"token_id": token_id})
        return resp if isinstance(resp, dict) else None
