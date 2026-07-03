from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from .db import SessionLocal, get_settings, prune_target_history
from .diff import compute_diff
from .extract import extract_relevant
from .models import Change, Crawl, Target
from .notify import send_email, send_telegram, send_whatsapp

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
    "BoatPulse/0.3"
)


async def _fetch(url: str) -> tuple[int, str, str | None]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        },
    ) as client:
        r = await client.get(url)
        return r.status_code, r.text, r.headers.get("content-type")


async def run_crawl(target_id: int) -> None:
    """Run a single crawl for a target and persist results. Notify on real change."""
    started = datetime.now(timezone.utc)
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            logger.warning("target %s not found", target_id)
            return
        crawl = Crawl(target_id=target.id, started_at=started)
        s.add(crawl)
        s.commit()
        s.refresh(crawl)
        crawl_id = crawl.id
        url = target.url
        selectors = target.selectors
        target_name = target.name
        last_hash = target.last_content_hash
        test_suffix = (get_settings(s).test_suffix or "").strip()

    http_status: int | None = None
    error_message: str | None = None
    extracted_text: str | None = None
    content_hash: str | None = None
    matched_selector: str | None = None

    try:
        http_status, body, content_type = await _fetch(url)
        if http_status >= 400:
            error_message = f"HTTP {http_status}"
        else:
            extraction = extract_relevant(body, selectors, content_type=content_type, test_suffix=test_suffix)
            extracted_text = extraction.text
            content_hash = extraction.content_hash
            matched_selector = (
                extraction.matched_selector
                or ("__fallback_body__" if extraction.used_fallback else None)
            )
    except Exception as exc:
        logger.exception("fetch/extract failed for %s", url)
        error_message = f"{type(exc).__name__}: {exc}"

    ok = error_message is None
    finished = datetime.now(timezone.utc)

    prev_crawl_id: int | None = None
    diff_to_notify: str | None = None

    with SessionLocal() as s:
        crawl = s.get(Crawl, crawl_id)
        target = s.get(Target, target_id)
        if crawl is None or target is None:
            return

        crawl.finished_at = finished
        crawl.http_status = http_status
        crawl.ok = ok
        crawl.error_message = error_message
        crawl.content_hash = content_hash
        crawl.extracted_text = extracted_text
        crawl.matched_selector = matched_selector

        target.last_crawl_at = finished
        target.last_status = "ok" if ok else "error"

        change_id: int | None = None
        if ok and content_hash is not None:
            if last_hash is None:
                target.last_content_hash = content_hash
            elif last_hash != content_hash:
                prev_crawl = s.execute(
                    select(Crawl)
                    .where(Crawl.target_id == target.id, Crawl.ok.is_(True), Crawl.id != crawl.id)
                    .order_by(Crawl.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                prev_text = prev_crawl.extracted_text if prev_crawl else None
                prev_crawl_id = prev_crawl.id if prev_crawl else None

                diff_result = compute_diff(prev_text, extracted_text or "")
                if diff_result.changed:
                    change = Change(
                        target_id=target.id,
                        crawl_id=crawl.id,
                        prev_crawl_id=prev_crawl_id,
                        unified_diff=diff_result.unified_diff or "(first comparison with stored text)",
                    )
                    s.add(change)
                    s.flush()
                    diff_to_notify = change.unified_diff
                    change_id = change.id
                target.last_content_hash = content_hash

        s.commit()

    # Trim history right after persistence so the DB stays bounded. Runs once
    # per crawl per target — no separate scheduled job needed.
    try:
        deleted_crawls, deleted_changes = prune_target_history(target_id)
        if deleted_crawls or deleted_changes:
            logger.info(
                "pruned target %s: -%s crawls, -%s changes",
                target_id, deleted_crawls, deleted_changes,
            )
    except Exception:
        logger.exception("prune failed for target %s", target_id)

    if change_id is None or diff_to_notify is None:
        return

    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            return
        title = f"Änderung erkannt: {target_name}"
        target_url = target.url
    tg_res, mail_res, wa_res = await asyncio.gather(
        send_telegram(title=title, url=target_url, body=diff_to_notify),
        send_email(subject=title, title=title, url=target_url, body=diff_to_notify),
        send_whatsapp(title=title, url=target_url, body=diff_to_notify),
    )
    with SessionLocal() as s:
        change = s.get(Change, change_id)
        if change is not None:
            change.notified_telegram = tg_res.ok
            change.notified_email = mail_res.ok
            change.notified_whatsapp = wa_res.ok
            err_parts = []
            if not tg_res.ok and tg_res.error:
                err_parts.append(f"telegram: {tg_res.error}")
            if not mail_res.ok and mail_res.error:
                err_parts.append(f"email: {mail_res.error}")
            if not wa_res.ok and wa_res.error:
                err_parts.append(f"whatsapp: {wa_res.error}")
            change.notify_error = " | ".join(err_parts) or None
            s.commit()
