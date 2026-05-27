"""
Thin HTTP client for the Polymarket surfaces we care about.

Layered on top of stdlib `urllib`. No third-party deps.

Endpoints (all read-only, no auth):
  * Leaderboard:   GET  {lb}/profit | /volume   ?window=Day|Week|Month|All&limit&offset
  * Data API:      GET  {data}/trades   ?user=<addr>&limit&offset
                   GET  {data}/positions ?user=<addr>
                   GET  {data}/value     ?user=<addr>
  * Gamma:         GET  {gamma}/markets  ?condition_ids=<id1,id2>...
                   GET  {gamma}/markets  ?closed=true&limit&offset
  * CLOB:          GET  {clob}/book      ?token_id=<id>
                   POST {clob}/books     [{token_id}...]
                   GET  {clob}/midpoint  ?token_id=<id>

Schemas drift over time. We always defensively .get() and never index by position.
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

log = logging.getLogger("war.api")


class ApiClient:
    def __init__(self, cfg: ApiConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------
    def _request(self, url: str, *, method: str = "GET", body: bytes | None = None,
                 headers: dict | None = None) -> Any:
        h = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        if headers:
            h.update(headers)
        if body is not None:
            h.setdefault("Content-Type", "application/json")
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
                # 429 / 5xx → backoff and retry; 4xx → give up
                code = e.code
                msg = ""
                try:
                    msg = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                if code in (429,) or 500 <= code < 600:
                    last_err = e
                    sleep_for = self.cfg.sleep_between * (2 ** attempt)
                    log.warning("HTTP %s on %s (try %d) — sleeping %.1fs",
                                code, url, attempt + 1, sleep_for)
                    time.sleep(sleep_for)
                    continue
                log.warning("HTTP %s on %s: %s", code, url, msg)
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                sleep_for = self.cfg.sleep_between * (2 ** attempt)
                log.warning("net err %s (try %d) — sleeping %.1fs", e, attempt + 1, sleep_for)
                time.sleep(sleep_for)
            except Exception as e:                                # noqa: BLE001
                last_err = e
                log.warning("unexpected err on %s: %s", url, e)
                return None
        log.error("giving up on %s after %d tries: %s", url, self.cfg.max_retries, last_err)
        return None

    def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        out = self._request(url)
        time.sleep(self.cfg.sleep_between)
        return out

    def _post(self, base: str, path: str, payload: Any) -> Any:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        body = json.dumps(payload).encode("utf-8")
        out = self._request(url, method="POST", body=body)
        time.sleep(self.cfg.sleep_between)
        return out

    @staticmethod
    def _unwrap(resp: Any) -> list:
        """Some endpoints wrap as {data:[...]} or {results:[...]}; some return bare list."""
        if resp is None:
            return []
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for k in ("data", "results", "items", "trades", "positions"):
                if k in resp and isinstance(resp[k], list):
                    return resp[k]
        return []

    # ------------------------------------------------------------------
    # leaderboard
    # ------------------------------------------------------------------
    def leaderboard(self, *, window: str = "30d", metric: str = "profit",
                    limit: int = 200, offset: int = 0) -> list[dict]:
        """
        Pull a page of the public leaderboard.

        `metric`: "profit" or "volume"
        `window`: "1d" | "7d" | "30d" | "all"   (the API rejects "Day"/"Month")
        Production payload shape (verified): a bare list of
            {proxyWallet, name, pseudonym, amount, bio, profileImage, ...}
        Older fields like `address` are also handled defensively in case the
        API moves them around again.
        """
        path = "/" + metric.strip("/")
        # NB: this endpoint does NOT support `offset`. We just over-fetch
        # `limit` and let callers slice client-side.
        resp = self._get(self.cfg.leaderboard_url, path,
                         {"window": window, "limit": limit})
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # data-api: per-user
    # ------------------------------------------------------------------
    def user_trades(self, address: str, *, limit: int = 500, offset: int = 0) -> list[dict]:
        resp = self._get(self.cfg.data_url, "/trades",
                         {"user": address, "limit": limit, "offset": offset})
        return self._unwrap(resp)

    def user_trades_all(self, address: str, *, page_size: int = 500, hard_cap: int = 5_000) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while offset < hard_cap:
            batch = self.user_trades(address, limit=page_size, offset=offset)
            if not batch:
                break
            out += batch
            if len(batch) < page_size:
                break
            offset += page_size
        return out

    def user_positions(self, address: str) -> list[dict]:
        resp = self._get(self.cfg.data_url, "/positions", {"user": address})
        return self._unwrap(resp)

    def user_value(self, address: str) -> dict | None:
        resp = self._get(self.cfg.data_url, "/value", {"user": address})
        if isinstance(resp, list) and resp:
            return resp[0]
        return resp if isinstance(resp, dict) else None

    # ------------------------------------------------------------------
    # gamma: markets / events
    # ------------------------------------------------------------------
    def markets_by_condition_ids(self, condition_ids: list[str]) -> list[dict]:
        """
        Fetch market metadata for the given condition_ids (open + closed).

        IMPORTANT: gamma's /markets?condition_ids=X endpoint silently applies
        an implicit `closed=false` filter. Without an explicit `closed` param,
        any resolved market in your batch is dropped from the response — which
        makes downstream wallets look like they never had a settled trade.

        We therefore issue TWO queries (`closed=false` + `closed=true`) and
        merge by conditionId. Verified empirically: this gives 100% hit rate
        on a real wallet's trade history; the single-query path was missing
        roughly 18% of markets (the resolved ones — i.e. exactly the markets
        we need for PnL).
        """
        if not condition_ids:
            return []
        out: dict[str, dict] = {}
        for closed_flag in ("false", "true"):
            resp = self._get(self.cfg.gamma_url, "/markets", {
                "condition_ids": condition_ids,
                "limit": min(len(condition_ids), 500),
                "closed": closed_flag,
            })
            rows = resp if isinstance(resp, list) else self._unwrap(resp)
            for m in rows:
                cid = m.get("conditionId") or m.get("condition_id")
                if cid:
                    out[cid] = m
        return list(out.values())

    def list_recently_closed_markets(self, *, limit: int = 500, offset: int = 0,
                                     min_volume: float = 0.0) -> list[dict]:
        """Closed markets sorted by recency. We filter by min_volume client-side."""
        resp = self._get(self.cfg.gamma_url, "/markets",
                         {"closed": "true", "limit": limit, "offset": offset,
                          "order": "endDate", "ascending": "false"})
        rows = resp if isinstance(resp, list) else self._unwrap(resp)
        out = []
        for m in rows:
            try:
                vol = float(m.get("volume") or m.get("volumeNum") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol >= min_volume:
                out.append(m)
        return out

    # ------------------------------------------------------------------
    # CLOB: orderbook
    # ------------------------------------------------------------------
    def book(self, token_id: str) -> dict | None:
        resp = self._get(self.cfg.clob_url, "/book", {"token_id": token_id})
        return resp if isinstance(resp, dict) else None

    def books_batch(self, token_ids: list[str]) -> list[dict]:
        if not token_ids:
            return []
        payload = [{"token_id": t} for t in token_ids]
        resp = self._post(self.cfg.clob_url, "/books", payload)
        return resp if isinstance(resp, list) else self._unwrap(resp)

    def midpoint(self, token_id: str) -> float | None:
        resp = self._get(self.cfg.clob_url, "/midpoint", {"token_id": token_id})
        if isinstance(resp, dict) and "mid" in resp:
            try:
                return float(resp["mid"])
            except (TypeError, ValueError):
                return None
        return None
