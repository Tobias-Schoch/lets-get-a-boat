from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass

import httpx

from .config import config

logger = logging.getLogger(__name__)

TELEGRAM_MAX = 4000
DIFF_PREVIEW = 3500
WHATSAPP_MAX = 1500  # CallMeBot truncates long messages; keep the diff short


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class Creds:
    """Effective notification config: DB settings first, env vars as fallback."""

    telegram_bot_token: str
    telegram_chat_id: str
    resend_api_key: str
    resend_from: str
    resend_to: str
    whatsapp_recipients: str


def _first(*vals: str | None) -> str:
    for v in vals:
        v = (v or "").strip()
        if v:
            return v
    return ""


def get_creds() -> Creds:
    from .db import SessionLocal, get_settings

    with SessionLocal() as s:
        st = get_settings(s)
        return Creds(
            telegram_bot_token=_first(st.telegram_bot_token, config.telegram_bot_token),
            telegram_chat_id=_first(st.telegram_chat_id, config.telegram_chat_id),
            resend_api_key=_first(st.resend_api_key, config.resend_api_key),
            resend_from=_first(st.resend_from),
            resend_to=_first(st.resend_to),
            whatsapp_recipients=(st.whatsapp_recipients or "").strip(),
        )


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def parse_addresses(raw: str | None) -> list[str]:
    """Split a user-entered recipient string on comma/semicolon/newline."""
    return [p.strip() for p in re.split(r"[,;\n]", raw or "") if p.strip()]


def parse_whatsapp_recipients(raw: str | None) -> list[tuple[str, str]]:
    """Parse ``telefon:apikey`` pairs, one per line (``,`` also accepted).

    Returns (phone, apikey) tuples. Spaces inside the phone number are
    stripped so ``+49 170 1234567`` works too.
    """
    out: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[:,]", line, maxsplit=1)
        if len(parts) != 2:
            continue
        phone = re.sub(r"\s+", "", parts[0])
        apikey = parts[1].strip()
        if phone and apikey:
            out.append((phone, apikey))
    return out


async def send_telegram(*, title: str, url: str | None, body: str) -> SendResult:
    creds = get_creds()
    token = creds.telegram_bot_token
    chat_id = creds.telegram_chat_id
    if not token or not chat_id:
        return SendResult(ok=False, error="Telegram bot-token oder chat-id nicht gesetzt")

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


async def send_email(*, subject: str, title: str, url: str | None, body: str) -> SendResult:
    creds = get_creds()
    api_key = creds.resend_api_key
    from_addr = creds.resend_from
    to_addrs = parse_addresses(creds.resend_to)
    if not api_key:
        return SendResult(ok=False, error="Resend api-key nicht gesetzt")
    if not from_addr or not to_addrs:
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
        "to": to_addrs,
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


async def _send_whatsapp_one(client: httpx.AsyncClient, phone: str, apikey: str, text: str) -> str | None:
    """Send to a single CallMeBot recipient. Returns an error string or None."""
    try:
        r = await client.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "apikey": apikey, "text": text},
        )
    except Exception as exc:
        return f"{phone}: {exc}"
    # CallMeBot signals problems either via status code or as text in a 200 page.
    body_low = r.text.lower()
    if r.status_code != 200 or "apikey is invalid" in body_low or "api key not valid" in body_low:
        return f"{phone}: http {r.status_code}: {r.text[:150]}"
    return None


async def send_whatsapp(*, title: str, url: str | None, body: str) -> SendResult:
    pairs = parse_whatsapp_recipients(get_creds().whatsapp_recipients)
    if not pairs:
        return SendResult(ok=False, error="WhatsApp-Empfänger in Settings nicht gesetzt")

    url_line = f"\n{url}" if url else ""
    text = _trim(f"{title}{url_line}\n\n{body}", WHATSAPP_MAX)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            errors = await asyncio.gather(
                *(_send_whatsapp_one(client, phone, apikey, text) for phone, apikey in pairs)
            )
    except Exception as exc:
        logger.exception("whatsapp send failed")
        return SendResult(ok=False, error=f"whatsapp exception: {exc}")

    failed = [e for e in errors if e]
    if failed:
        return SendResult(ok=False, error="whatsapp: " + " | ".join(failed))
    return SendResult(ok=True)


async def test_telegram() -> SendResult:
    return await send_telegram(
        title="Boat Pulse – Telegram-Test",
        url=None,
        body="Wenn du das liest, funktioniert dein Telegram-Bot. 🚤",
    )


async def test_email() -> SendResult:
    return await send_email(
        subject="Boat Pulse – Email-Test",
        title="Boat Pulse – Email-Test",
        url=None,
        body="Wenn du das liest, funktioniert dein Resend-Setup.",
    )


async def test_whatsapp() -> SendResult:
    return await send_whatsapp(
        title="Boat Pulse – WhatsApp-Test",
        url=None,
        body="Wenn du das liest, funktioniert dein CallMeBot-Setup. 🚤",
    )
