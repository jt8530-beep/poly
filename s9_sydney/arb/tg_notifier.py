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
        mode = "纸面" if paper else "实盘"
        msg = (
          f"<b>[{_esc(mode)}] 套利信号：{_esc(strategy)}</b>\n"
          f"边际优势：<b>{sig.edge_bps} bps</b>\n"
          f"事件：<code>{_esc(sig.event_slug)}</code>\n"
          f"{_esc(sig.event_title[:140])}\n\n"
          f"A 腿：<code>{_esc(sig.leg_a.action_str)}</code>\n"
          f"问题：{_esc(sig.leg_a.question[:80])}\n"
          f"B 腿：<code>{_esc(sig.leg_b.action_str)}</code>\n"
          f"问题：{_esc(sig.leg_b.question[:80])}\n\n"
          f"共享数量：<b>{sig.q_shares:.2f}</b> 份\n"
          f"投入资金：<b>${sig.max_size_usd:.2f}</b>\n"
          f"预估利润：<b>${sig.expected_profit_usd:.2f}</b>\n"
          f"<i>{_esc(sig.notes)}</i>"
        )
        self.send(msg)

    def heartbeat(self, summary: str):
        try:
            data = json.loads(summary)
        except Exception:
            self.send(f"<b>心跳</b> {_esc(summary)}")
            return

        parts = []
        if "events_scanned" in data:
            parts.append(f"事件 {data.get('events_scanned')}")
        if "markets_scanned" in data:
            parts.append(f"市场 {data.get('markets_scanned')}")
        if "signals" in data:
            parts.append(f"信号 {data.get('signals')}")
        if "executed_paper" in data:
            parts.append(f"纸面执行 {data.get('executed_paper')}")
        if "ladder_positive" in data:
            parts.append(f"阶梯候选 {data.get('ladder_positive')}")
        if "neg_risk_positive" in data:
            parts.append(f"负风险候选 {data.get('neg_risk_positive')}")
        if "actionable_positive" in data:
            parts.append(f"可执行候选 {data.get('actionable_positive')}")
        if "same_market_positive" in data:
            parts.append(f"同市场候选 {data.get('same_market_positive')}")
        if "best_neg_risk_bps" in data:
            parts.append(f"最佳负风险 {data.get('best_neg_risk_bps')}bps")
        if "best_actionable_bps" in data:
            parts.append(f"最佳可执行 {data.get('best_actionable_bps')}bps")
        if "best_ladder_bps" in data:
            parts.append(f"最佳阶梯 {data.get('best_ladder_bps')}bps")
        if "elapsed_sec" in data:
            parts.append(f"耗时 {data.get('elapsed_sec')}s")
        self.send("<b>心跳</b> " + _esc("；".join(parts) if parts else summary))

    def halt(self, reason: str):
        self.send(f"<b>已暂停</b>：{_esc(reason)}")
