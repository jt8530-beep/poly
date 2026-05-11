"""
Telegram notifications via Bot API.

v1: switched from Markdown (fragile with _ [ ] ` in slugs/questions) to
HTML parse_mode with proper escaping. All user-supplied strings go
through _esc(). Zero third-party deps.
"""
from __future__ import annotations
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("tg")


def _esc(s: str) -> str:
    if s is None: return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


class Notifier:
    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token   = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            log.debug("tg disabled, would have sent: %s", text[:200])
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
                return bool(j.get("ok"))
        except Exception as e:
            log.warning("tg send failed: %s", e)
            return False

    def signal(self, sig, strategy: str, paper: bool):
        mode = "PAPER" if paper else "LIVE"
        msg = (
          f"<b>[{_esc(mode)}] {_esc(strategy)}</b>  edge <b>{sig.edge_bps} bps</b>\n"
          f"event: <code>{_esc(sig.event_slug)}</code>\n"
          f"{_esc(sig.event_title[:140])}\n\n"
          f"leg A: <code>{_esc(sig.leg_a.action_str)}</code>\n"
          f"     on: {_esc(sig.leg_a.question[:80])}\n"
          f"leg B: <code>{_esc(sig.leg_b.action_str)}</code>\n"
          f"     on: {_esc(sig.leg_b.question[:80])}\n\n"
          f"shared q = <b>{sig.q_shares:.2f}</b> shares\n"
          f"capital in: <b>${sig.max_size_usd:.2f}</b>   "
          f"expected profit: <b>${sig.expected_profit_usd:.2f}</b>\n"
          f"<i>{_esc(sig.notes)}</i>"
        )
        self.send(msg)

    def heartbeat(self, summary: str):
        self.send(f"<code>hb</code> {_esc(summary)}")

    def halt(self, reason: str):
        self.send(f"<b>HALT</b>: {_esc(reason)}")
