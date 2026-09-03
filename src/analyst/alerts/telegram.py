"""Telegram delivery. Absent credentials disable the channel, never crash it."""
from __future__ import annotations

import logging

import httpx

from analyst.core.clock import now_utc
from analyst.core.config import Secrets
from analyst.core.models import AnalysisResult
from analyst.storage.db import session_scope
from analyst.storage.models import AlertLog

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, secrets: Secrets | None = None, timeout: float = 15.0) -> None:
        self.secrets = secrets or Secrets.from_env()
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return self.secrets.telegram_ready

    def send(self, text: str) -> tuple[bool, str]:
        if not self.enabled:
            return False, "TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوط"
        try:
            resp = httpx.post(
                _API.format(token=self.secrets.telegram_bot_token),
                json={
                    "chat_id": self.secrets.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            return False, f"فشل الاتصال بتيليجرام: {exc}"

        if resp.status_code != 200:
            return False, f"تيليجرام رفض الرسالة ({resp.status_code}): {resp.text[:200]}"
        return True, "تم الإرسال"

    def send_alert(self, result: AnalysisResult, text: str) -> bool:
        """Send and log. The log row is what dedupe reads on the next run."""
        ok, detail = self.send(text)
        with session_scope() as session:
            session.add(
                AlertLog(
                    symbol=result.symbol,
                    channel="telegram",
                    sent_at=now_utc(),
                    direction=int(result.direction.value),
                    grade=result.grade.value,
                    confidence=result.confidence,
                    ok=ok,
                    detail=detail,
                )
            )
        if not ok:
            log.warning("تعذّر إرسال تنبيه %s: %s", result.symbol, detail)
        return ok
