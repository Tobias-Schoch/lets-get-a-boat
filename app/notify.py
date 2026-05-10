from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import httpx

from .config import config

logger = logging.getLogger(__name__)

TELEGRAM_MAX = 4000
DIFF_PREVIEW = 3500


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


async def send_telegram(*, title: str, url: str | None, body: str) -> SendResult:
    token = (config.telegram_bot_token or "").strip()
    chat_id = (config.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return SendResult(ok=False, error="TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env var missing")

    safe_title = html.escape(title)
    safe_body = html.escape(_trim(body, DIFF_PREVIEW))
    url_line = f'\n<a href="{html.escape(url)}">{html.escape(url)}</a>' if url else ""
    message = f"<b>{safe_title}</b>{url_line}\n<pre>{safe_body}</pre>"
    message = _trim(message, TELEGRAM_MAX)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "text": message,
                },
            )
        if r.status_code != 200:
            return SendResult(ok=False, error=f"telegram http {r.status_code}: {r.text[:200]}")
        return SendResult(ok=True)
    except Exception as exc:
        logger.exception("telegram send failed")
        return SendResult(ok=False, error=f"telegram exception: {exc}")


async def send_email(
    *,
    subject: str,
    title: str,
    url: str | None,
    body: str,
    from_addr: str,
    to_addr: str,
) -> SendResult:
    api_key = (config.resend_api_key or "").strip()
    from_addr = (from_addr or "").strip()
    to_addr = (to_addr or "").strip()
    if not api_key:
        return SendResult(ok=False, error="RESEND_API_KEY env var missing")
    if not from_addr or not to_addr:
        return SendResult(ok=False, error="From- oder An-Adresse in Settings nicht gesetzt")

    link_html = (
        f'<p><a href="{html.escape(url)}">{html.escape(url)}</a></p>' if url else ""
    )
    body_html = (
        f"<h2>{html.escape(title)}</h2>"
        f"{link_html}"
        f"<pre style=\"font-family: ui-monospace,Menlo,Consolas,monospace; "
        f"white-space: pre-wrap; word-break: break-word; background:#f6f8fa; "
        f"padding:12px; border-radius:6px;\">{html.escape(body)}</pre>"
    )

    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": body_html,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        if r.status_code >= 300:
            return SendResult(ok=False, error=f"resend http {r.status_code}: {r.text[:300]}")
        return SendResult(ok=True)
    except Exception as exc:
        logger.exception("resend send failed")
        return SendResult(ok=False, error=f"resend exception: {exc}")


async def test_telegram() -> SendResult:
    return await send_telegram(
        title="Boat Pulse – Telegram-Test",
        url=None,
        body="Wenn du das liest, funktioniert dein Telegram-Bot. 🚤",
    )


async def test_email(*, from_addr: str, to_addr: str) -> SendResult:
    return await send_email(
        subject="Boat Pulse – Email-Test",
        title="Boat Pulse – Email-Test",
        url=None,
        body="Wenn du das liest, funktioniert dein Resend-Setup.",
        from_addr=from_addr,
        to_addr=to_addr,
    )
