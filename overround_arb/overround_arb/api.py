"""
Minimal Polymarket client for the scanner.

Only the surfaces we need:
  * gamma /events?closed=false&limit=N&offset=N&order=...  (paged)
  * gamma /events?slug=<slug> (single)

Defensive HTTP with retry on 429 / 5xx. Stdlib only.
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

log = logging.getLogger("oa.api")


class ApiClient:
    def __init__(self, cfg: ApiConfig):
        self.cfg = cfg

    def _request(self, url: str, *, method: str = "GET",
                 body: bytes | None = None) -> Any:
        h = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        last_err: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as r:
                    raw = r.read().decode("utf-8", "replace")
                    if not raw:
                        return None
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    last_err = e
                    sleep_for = self.cfg.sleep_between * (2 ** attempt)
                    log.warning("HTTP %s on %s (try %d) — sleeping %.1fs",
                                e.code, url, attempt + 1, sleep_for)
                    time.sleep(sleep_for)
                    continue
                log.warning("HTTP %s on %s", e.code, url)
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                sleep_for = self.cfg.sleep_between * (2 ** attempt)
                log.warning("net err %s (try %d)", e, attempt + 1)
                time.sleep(sleep_for)
            except Exception as e:                                # noqa: BLE001
                log.warning("unexpected err on %s: %s", url, e)
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

    # ------------------------------------------------------------------
    # gamma /events
    # ------------------------------------------------------------------
    def list_events_page(self, *, closed: bool = False, limit: int = 100,
                         offset: int = 0, order: str = "volume",
                         ascending: bool = False) -> list[dict]:
        """
        Fetch one page of events. Each event includes a `markets` list with
        nested market objects, so we don't need a second call per event.
        """
        resp = self._get(self.cfg.gamma_url, "/events", {
            "closed": "true" if closed else "false",
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": "true" if ascending else "false",
        })
        return resp if isinstance(resp, list) else []

    def iter_events(self, *, page_size: int = 100, max_pages: int = 20,
                    closed: bool = False) -> list[dict]:
        """Pull up to (page_size * max_pages) most-recent events."""
        out: list[dict] = []
        for page in range(max_pages):
            batch = self.list_events_page(closed=closed,
                                          limit=page_size,
                                          offset=page * page_size)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out

    def get_event_by_slug(self, slug: str) -> dict | None:
        resp = self._get(self.cfg.gamma_url, "/events", {"slug": slug, "closed": "false"})
        if isinstance(resp, list) and resp:
            return resp[0]
        return None
