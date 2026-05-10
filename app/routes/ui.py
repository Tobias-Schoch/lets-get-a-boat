from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, update

from ..auth import require_basic_auth
from ..config import config
from ..crawler import run_crawl
from ..db import SessionLocal, get_settings
from ..diff import compute_diff
from ..extract import extract_relevant
from ..models import Change, Crawl, Settings, Target
from ..notify import send_email, send_telegram, test_email, test_telegram
from ..scheduler import (
    DEFAULT_INTERVAL_MINUTES,
    get_scheduler,
    global_interval_minutes,
    next_run_at,
    remove_job,
    reschedule_all,
    upsert_job,
)

router = APIRouter(dependencies=[Depends(require_basic_auth)])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PULSE_LENGTH = 24  # ticks shown in the sparkline — also caps detail history tables

try:
    DISPLAY_TZ = ZoneInfo(config.tz or "Europe/Berlin")
except Exception:
    DISPLAY_TZ = ZoneInfo("Europe/Berlin")


def _ensure_utc(dt: datetime) -> datetime:
    """Stamp naive datetimes as UTC. SQLite roundtrip drops tzinfo on read."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _ensure_utc(dt).astimezone(DISPLAY_TZ)


def to_iso_utc(dt: datetime | None) -> str:
    """Serialize a datetime with explicit UTC suffix so JS Date.parse stays correct."""
    if dt is None:
        return ""
    return _ensure_utc(dt).isoformat()


templates.env.filters["local"] = to_local
templates.env.filters["iso_utc"] = to_iso_utc


def _fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _system_stats(session) -> dict:
    import time as _time
    from ..main import APP_STARTED_MONO

    db_path = Path(config.data_dir) / "boat.db"
    db_size = db_path.stat().st_size if db_path.exists() else 0

    target_count = session.execute(select(func.count(Target.id))).scalar_one() or 0
    crawl_count = session.execute(select(func.count(Crawl.id))).scalar_one() or 0
    change_count = session.execute(select(func.count(Change.id))).scalar_one() or 0
    settings_count = session.execute(select(func.count(Settings.id))).scalar_one() or 0
    jobs = len(get_scheduler().get_jobs()) if get_scheduler().running else 0

    uptime_s = _time.monotonic() - APP_STARTED_MONO

    return {
        "db_size_bytes": db_size,
        "db_size_human": _fmt_size(db_size),
        "uptime_seconds": uptime_s,
        "uptime_human": _fmt_uptime(uptime_s),
        "scheduler_jobs": jobs,
        "scheduler_running": get_scheduler().running,
        "tables": {
            "target": target_count,
            "crawl": crawl_count,
            "change": change_count,
            "settings": settings_count,
        },
    }


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


def _env_status() -> list[dict[str, object]]:
    """All boat-pulse env vars in display order with their present/missing state."""
    fields = [
        ("TELEGRAM_BOT_TOKEN", config.telegram_bot_token),
        ("TELEGRAM_CHAT_ID", config.telegram_chat_id),
        ("RESEND_API_KEY", config.resend_api_key),
        ("BASIC_AUTH_USER", config.basic_auth_user),
        ("BASIC_AUTH_PASSWORD", config.basic_auth_password),
        ("DATA_DIR", config.data_dir),
        ("TZ", config.tz),
    ]
    return [{"key": k, "present": bool((v or "").strip())} for k, v in fields]


def _is_real_notify_error(notify_error: str | None) -> bool:
    """Treat 'creds missing' messages as 'not configured', not as a real failure."""
    if not notify_error:
        return False
    parts = [p.strip() for p in notify_error.split("|") if p.strip()]
    for p in parts:
        low = p.lower()
        if "missing" in low or "nicht gesetzt" in low:
            continue
        return True
    return False


def _pulse_from_crawls(crawls, changes_by_crawl_id: dict | None = None) -> list[str]:
    """Return crawl statuses oldest→newest for the sparkline strip.

    States:
    - ``pending`` (orange): crawl in progress
    - ``fail``    (red):    HTTP/parse error on the page OR real notify failure
                            (telegram/email returned an error — *not* just missing creds)
    - ``change``  (blue):   crawl ok, content changed (notifications delivered or skipped)
    - ``ok``      (green):  crawl ok, content identical
    """
    cmap = changes_by_crawl_id or {}
    out: list[str] = []
    for c in reversed(crawls):
        if c.finished_at is None:
            out.append("pending")
            continue
        if not c.ok:
            out.append("fail")
            continue
        change = cmap.get(c.id)
        if change is None:
            out.append("ok")
        elif _is_real_notify_error(change.notify_error):
            out.append("fail")
        else:
            out.append("change")
    return out


def _changes_for_crawls(session, crawl_ids: list[int]) -> dict:
    if not crawl_ids:
        return {}
    rows = (
        session.execute(select(Change).where(Change.crawl_id.in_(crawl_ids)))
        .scalars()
        .all()
    )
    return {ch.crawl_id: ch for ch in rows}


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
                    .limit(PULSE_LENGTH)
                )
                .scalars()
                .all()
            )
            changes_map = _changes_for_crawls(s, [c.id for c in recent])
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
                    "pulse": _pulse_from_crawls(recent, changes_map),
                    "next_run_at": next_run_at(t.id) if t.enabled else None,
                }
            )

    with SessionLocal() as s2:
        system = _system_stats(s2)

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
            "system": system,
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
                select(Crawl).where(Crawl.target_id == target_id).order_by(Crawl.id.desc()).limit(PULSE_LENGTH)
            )
            .scalars()
            .all()
        )
        changes = (
            s.execute(
                select(Change).where(Change.target_id == target_id).order_by(Change.id.desc()).limit(PULSE_LENGTH)
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
            "pulse": _pulse_from_crawls(crawls, _changes_for_crawls(s, [c.id for c in crawls])),
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
                select(Crawl).where(Crawl.target_id == target_id).order_by(Crawl.id.desc()).limit(PULSE_LENGTH)
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
        changes_map = _changes_for_crawls(s, [c.id for c in crawls])
    return {
        "ok": True,
        "last_status": target.last_status,
        "last_crawl_at": to_iso_utc(target.last_crawl_at),
        "change_count": change_count,
        "had_change": change_count > before_change_count,
        "pulse": _pulse_from_crawls(crawls, changes_map),
        "next_run_at": to_iso_utc(nrun),
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
                "test_suffix": settings.test_suffix or "",
                "updated_at": settings.updated_at,
            },
            "env_vars": _env_status(),
            "flash": _flash(request),
        }
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
def settings_save(
    crawl_interval_minutes: int = Form(DEFAULT_INTERVAL_MINUTES),
    resend_from: str = Form(""),
    resend_to: str = Form(""),
    test_suffix: str = Form(""),
):
    with SessionLocal() as s:
        settings = get_settings(s)
        settings.crawl_interval_minutes = max(1, int(crawl_interval_minutes))
        settings.resend_from = resend_from.strip() or None
        settings.resend_to = resend_to.strip() or None
        settings.test_suffix = test_suffix.strip() or None
        s.commit()
    reschedule_all()
    return _flash_redirect("/settings", "ok", "Einstellungen gespeichert")


@router.post("/settings/wipe-history")
def settings_wipe_history():
    """Delete all crawls + changes, reset target.last_* fields. Targets + settings stay."""
    with SessionLocal() as s:
        # Important: clear FK references first.
        s.execute(delete(Change))
        s.execute(delete(Crawl))
        s.execute(
            update(Target).values(
                last_crawl_at=None,
                last_status=None,
                last_content_hash=None,
            )
        )
        s.commit()
    # VACUUM shrinks the SQLite file after big deletes — outside of any transaction.
    from ..db import engine
    with engine.connect() as conn:
        conn.exec_driver_sql("VACUUM")
    return _flash_redirect("/settings", "ok", "Verlauf gelöscht (crawls + änderungen)")


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
