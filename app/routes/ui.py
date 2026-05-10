from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..auth import require_basic_auth
from ..config import config
from ..crawler import run_crawl
from ..db import SessionLocal, get_settings
from ..diff import compute_diff
from ..extract import extract_relevant
from ..models import Change, Crawl, Target
from ..notify import send_email, send_telegram, test_email, test_telegram
from ..scheduler import (
    DEFAULT_INTERVAL_MINUTES,
    global_interval_minutes,
    next_run_at,
    remove_job,
    reschedule_all,
    upsert_job,
)

router = APIRouter(dependencies=[Depends(require_basic_auth)])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _flash_redirect(path: str, kind: str, message: str) -> RedirectResponse:
    q = urlencode({"flash": kind, "msg": message})
    return RedirectResponse(f"{path}?{q}", status_code=303)


def _flash(request: Request) -> dict | None:
    kind = request.query_params.get("flash")
    msg = request.query_params.get("msg")
    if kind and msg:
        return {"kind": kind, "msg": msg}
    return None


def _parse_selectors(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _credential_status() -> dict[str, bool]:
    return {
        "telegram_bot_token": bool((config.telegram_bot_token or "").strip()),
        "telegram_chat_id": bool((config.telegram_chat_id or "").strip()),
        "resend_api_key": bool((config.resend_api_key or "").strip()),
    }


def _pulse_from_crawls(crawls) -> list[str]:
    """Return crawl statuses oldest→newest for the sparkline strip."""
    out: list[str] = []
    for c in reversed(crawls):
        if c.finished_at is None:
            out.append("pending")
        elif c.ok:
            out.append("ok")
        else:
            out.append("fail")
    return out


@router.get("/", response_class=HTMLResponse)
def overview(request: Request):
    with SessionLocal() as s:
        targets = s.execute(select(Target).order_by(Target.id)).scalars().all()

        target_rows = []
        total_changes = 0
        total_crawls = s.execute(select(func.count(Crawl.id))).scalar_one() or 0
        active_targets = 0
        last_change_at: datetime | None = None
        last_crawl_at: datetime | None = None

        for t in targets:
            recent = (
                s.execute(
                    select(Crawl)
                    .where(Crawl.target_id == t.id)
                    .order_by(Crawl.id.desc())
                    .limit(24)
                )
                .scalars()
                .all()
            )
            change_count = (
                s.execute(
                    select(func.count(Change.id)).where(Change.target_id == t.id)
                ).scalar_one()
                or 0
            )
            last_change = s.execute(
                select(Change.created_at)
                .where(Change.target_id == t.id)
                .order_by(Change.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            total_changes += change_count
            if t.enabled:
                active_targets += 1
            if t.last_crawl_at and (last_crawl_at is None or t.last_crawl_at > last_crawl_at):
                last_crawl_at = t.last_crawl_at
            if last_change and (last_change_at is None or last_change > last_change_at):
                last_change_at = last_change

            target_rows.append(
                {
                    "id": t.id,
                    "name": t.name,
                    "url": t.url,
                    "selectors": t.selectors,
                    "enabled": t.enabled,
                    "last_crawl_at": t.last_crawl_at,
                    "last_status": t.last_status,
                    "last_content_hash": t.last_content_hash,
                    "change_count": change_count,
                    "last_change_at": last_change,
                    "pulse": _pulse_from_crawls(recent),
                    "next_run_at": next_run_at(t.id) if t.enabled else None,
                }
            )

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "targets": target_rows,
            "interval_minutes": global_interval_minutes(),
            "stats": {
                "targets": len(target_rows),
                "active": active_targets,
                "changes": total_changes,
                "crawls": total_crawls,
                "last_change_at": last_change_at,
                "last_crawl_at": last_crawl_at,
            },
            "flash": _flash(request),
            "now": datetime.now(timezone.utc),
        },
    )


@router.get("/targets/new", response_class=HTMLResponse)
def target_new(request: Request):
    return templates.TemplateResponse(
        request,
        "target_edit.html",
        {
            "target": None,
            "name": "",
            "url": "",
            "selectors_text": "",
            "enabled": True,
            "interval_minutes": global_interval_minutes(),
            "flash": _flash(request),
        },
    )


@router.post("/targets/new")
def target_create(
    name: str = Form(...),
    url: str = Form(...),
    selectors: str = Form(""),
    enabled: bool = Form(False),
):
    name = name.strip()
    url = url.strip()
    if not name or not url:
        return _flash_redirect("/targets/new", "error", "Name und URL sind erforderlich")
    interval = global_interval_minutes()
    with SessionLocal() as s:
        existing = s.execute(select(Target).where(Target.url == url)).scalar_one_or_none()
        if existing is not None:
            return _flash_redirect("/targets/new", "error", "Diese URL existiert bereits")
        t = Target(name=name, url=url, interval_minutes=interval, enabled=enabled)
        t.selectors = _parse_selectors(selectors)
        s.add(t)
        s.commit()
        s.refresh(t)
        if t.enabled:
            upsert_job(t.id, interval)
    return _flash_redirect("/", "ok", "Target angelegt")


@router.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(target_id: int, request: Request):
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            raise HTTPException(404)
        crawls = (
            s.execute(
                select(Crawl).where(Crawl.target_id == target_id).order_by(Crawl.id.desc()).limit(50)
            )
            .scalars()
            .all()
        )
        changes = (
            s.execute(
                select(Change).where(Change.target_id == target_id).order_by(Change.id.desc()).limit(50)
            )
            .scalars()
            .all()
        )
        latest_text: str | None = None
        latest_ok_crawl_at = None
        for c in crawls:
            if c.ok:
                latest_text = c.extracted_text or ""
                latest_ok_crawl_at = c.finished_at or c.started_at
                break
        ctx = {
            "target": {
                "id": target.id,
                "name": target.name,
                "url": target.url,
                "selectors": target.selectors,
                "enabled": target.enabled,
                "last_crawl_at": target.last_crawl_at,
                "last_status": target.last_status,
            },
            "interval_minutes": global_interval_minutes(),
            "crawls": crawls,
            "changes": changes,
            "latest_text": latest_text,
            "latest_ok_crawl_at": latest_ok_crawl_at,
            "pulse": _pulse_from_crawls(crawls[:24]),
            "next_run_at": next_run_at(target.id) if target.enabled else None,
            "flash": _flash(request),
        }
    return templates.TemplateResponse(request, "target.html", ctx)


@router.get("/targets/{target_id}/edit", response_class=HTMLResponse)
def target_edit(target_id: int, request: Request):
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            raise HTTPException(404)
        ctx = {
            "target": target,
            "name": target.name,
            "url": target.url,
            "selectors_text": "\n".join(target.selectors),
            "enabled": target.enabled,
            "interval_minutes": global_interval_minutes(),
            "flash": _flash(request),
        }
    return templates.TemplateResponse(request, "target_edit.html", ctx)


@router.post("/targets/{target_id}/edit")
def target_update(
    target_id: int,
    name: str = Form(...),
    url: str = Form(...),
    selectors: str = Form(""),
    enabled: bool = Form(False),
):
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            raise HTTPException(404)
        target.name = name.strip() or target.name
        target.url = url.strip() or target.url
        target.selectors = _parse_selectors(selectors)
        target.enabled = enabled
        s.commit()
        if target.enabled:
            upsert_job(target.id)
        else:
            remove_job(target.id)
    return _flash_redirect(f"/targets/{target_id}", "ok", "Gespeichert")


def _target_state_json(target_id: int, before_change_count: int) -> dict:
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            return {"ok": False, "error": "target not found"}
        crawls = (
            s.execute(
                select(Crawl).where(Crawl.target_id == target_id).order_by(Crawl.id.desc()).limit(24)
            )
            .scalars()
            .all()
        )
        change_count = (
            s.execute(
                select(func.count(Change.id)).where(Change.target_id == target_id)
            ).scalar_one()
            or 0
        )
        latest_crawl = crawls[0] if crawls else None
        snapshot_text = next((c.extracted_text for c in crawls if c.ok), None) or ""
        nrun = next_run_at(target_id) if target.enabled else None
    return {
        "ok": True,
        "last_status": target.last_status,
        "last_crawl_at": target.last_crawl_at.isoformat() if target.last_crawl_at else None,
        "change_count": change_count,
        "had_change": change_count > before_change_count,
        "pulse": _pulse_from_crawls(crawls),
        "next_run_at": nrun.isoformat() if nrun else None,
        "matched_selector": latest_crawl.matched_selector if latest_crawl else None,
        "http_status": latest_crawl.http_status if latest_crawl else None,
        "error_message": latest_crawl.error_message if latest_crawl else None,
        "snapshot_text": snapshot_text,
    }


@router.post("/targets/{target_id}/crawl-now")
async def target_crawl_now(target_id: int, request: Request):
    with SessionLocal() as s:
        if s.get(Target, target_id) is None:
            raise HTTPException(404)
        before_changes = (
            s.execute(
                select(func.count(Change.id)).where(Change.target_id == target_id)
            ).scalar_one()
            or 0
        )

    await run_crawl(target_id)

    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse(_target_state_json(target_id, before_changes))
    return _flash_redirect(f"/targets/{target_id}", "ok", "Crawl abgeschlossen")


@router.post("/targets/{target_id}/toggle")
def target_toggle(target_id: int):
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            raise HTTPException(404)
        target.enabled = not target.enabled
        s.commit()
        if target.enabled:
            upsert_job(target.id)
        else:
            remove_job(target.id)
        state = "aktiviert" if target.enabled else "deaktiviert"
    return _flash_redirect("/", "ok", f"Target {state}")


@router.post("/targets/{target_id}/delete")
def target_delete(target_id: int):
    with SessionLocal() as s:
        target = s.get(Target, target_id)
        if target is None:
            raise HTTPException(404)
        s.delete(target)
        s.commit()
    remove_job(target_id)
    return _flash_redirect("/", "ok", "Target gelöscht")


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request):
    with SessionLocal() as s:
        settings = get_settings(s)
        ctx = {
            "settings": {
                "resend_from": settings.resend_from or "",
                "resend_to": settings.resend_to or "",
                "crawl_interval_minutes": settings.crawl_interval_minutes or DEFAULT_INTERVAL_MINUTES,
                "updated_at": settings.updated_at,
            },
            "creds": _credential_status(),
            "flash": _flash(request),
        }
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
def settings_save(
    crawl_interval_minutes: int = Form(DEFAULT_INTERVAL_MINUTES),
    resend_from: str = Form(""),
    resend_to: str = Form(""),
):
    with SessionLocal() as s:
        settings = get_settings(s)
        settings.crawl_interval_minutes = max(1, int(crawl_interval_minutes))
        settings.resend_from = resend_from.strip() or None
        settings.resend_to = resend_to.strip() or None
        s.commit()
    reschedule_all()
    return _flash_redirect("/settings", "ok", "Einstellungen gespeichert")


@router.post("/settings/test-telegram")
async def settings_test_telegram():
    res = await test_telegram()
    if res.ok:
        return _flash_redirect("/settings", "ok", "Telegram-Test gesendet")
    return _flash_redirect("/settings", "error", f"Telegram-Test fehlgeschlagen: {res.error}")


@router.post("/settings/test-email")
async def settings_test_email():
    with SessionLocal() as s:
        settings = get_settings(s)
        from_addr = settings.resend_from or ""
        to_addr = settings.resend_to or ""
    res = await test_email(from_addr=from_addr, to_addr=to_addr)
    if res.ok:
        return _flash_redirect("/settings", "ok", "Email-Test gesendet")
    return _flash_redirect("/settings", "error", f"Email-Test fehlgeschlagen: {res.error}")


@router.get("/tester", response_class=HTMLResponse)
def tester_view(request: Request):
    return templates.TemplateResponse(
        request,
        "tester.html",
        {
            "html_a": "",
            "html_b": "",
            "selectors_text": "",
            "result": None,
            "send_test": False,
            "flash": _flash(request),
        },
    )


@router.post("/tester", response_class=HTMLResponse)
async def tester_run(
    request: Request,
    html_a: str = Form(""),
    html_b: str = Form(""),
    selectors: str = Form(""),
    send_test: bool = Form(False),
):
    selectors_list = _parse_selectors(selectors)
    ext_a = extract_relevant(html_a, selectors_list)
    ext_b = extract_relevant(html_b, selectors_list)
    diff = compute_diff(ext_a.text, ext_b.text)
    would_notify = diff.changed and diff.reason != "first_seen"

    notify_status: dict[str, str] | None = None
    if send_test and would_notify:
        with SessionLocal() as s:
            settings = get_settings(s)
            from_addr = settings.resend_from or ""
            to_addr = settings.resend_to or ""
        title = "Boat Pulse – HTML-Tester (echter Versand)"
        body = diff.unified_diff or "(kein Diff-Text – Erstvergleich)"
        tg = await send_telegram(title=title, url=None, body=body)
        mail = await send_email(
            subject=title,
            title=title,
            url=None,
            body=body,
            from_addr=from_addr,
            to_addr=to_addr,
        )
        notify_status = {
            "telegram": "ok" if tg.ok else f"error: {tg.error}",
            "email": "ok" if mail.ok else f"error: {mail.error}",
        }

    return templates.TemplateResponse(
        request,
        "tester.html",
        {
            "html_a": html_a,
            "html_b": html_b,
            "selectors_text": selectors,
            "send_test": send_test,
            "result": {
                "text_a": ext_a.text,
                "text_b": ext_b.text,
                "matched_a": ext_a.matched_selector,
                "matched_b": ext_b.matched_selector,
                "fallback_a": ext_a.used_fallback,
                "fallback_b": ext_b.used_fallback,
                "hash_a": ext_a.content_hash,
                "hash_b": ext_b.content_hash,
                "diff": diff.unified_diff,
                "reason": diff.reason,
                "would_notify": would_notify,
                "notify_status": notify_status,
            },
            "flash": _flash(request),
        },
    )
