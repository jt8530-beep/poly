"""Telegram notifications via Bot API.

Zero deps: urllib only. If bot_token / chat_id unset, becomes a no-op.
"""
from __future__ import annotations
import json, logging, urllib.parse, urllib.request

log = logging.getLogger("tg")


class Notifier:
    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token   = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)

    def send(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.enabled:
            log.debug("tg disabled, would have sent: %s", text[:120])
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read()
                j = json.loads(body.decode("utf-8","replace"))
                return bool(j.get("ok"))
        except Exception as e:
            log.warning("tg send failed: %s", e)
            return False

    def signal(self, sig, strategy: str, paper: bool):
        mode = "PAPER" if paper else "LIVE"
        msg = (
          f"*[{mode}] {strategy} signal*  edge *{sig.edge_bps} bps*\n"
          f"event: `{sig.event_slug}`\n"
          f"{sig.event_title[:120]}\n\n"
          f"leg A ({sig.leg_a_market.question[:60]}): `{sig.leg_a_action}`\n"
          f"leg B ({sig.leg_b_market.question[:60]}): `{sig.leg_b_action}`\n\n"
          f"expected profit: *${sig.expected_profit_usd:.2f}*  "
          f"max size: ${sig.max_size_usd:.2f}\n"
          f"_{sig.notes}_"
        )
        self.send(msg)

    def heartbeat(self, summary: str):
        self.send(f"`hb` {summary}")

    def halt(self, reason: str):
        self.send(f"*HALT*: {reason}")
