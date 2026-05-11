from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Change, Target

router = APIRouter(prefix="/api", tags=["public"])


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@router.get("/changes")
def changes_status() -> dict:
    """Public, no-auth endpoint: have we observed any page changes yet?"""
    with SessionLocal() as s:
        targets = s.execute(select(Target).order_by(Target.id)).scalars().all()

        target_rows = []
        total_changes = 0
        overall_last_change_at: datetime | None = None

        for t in targets:
            change_count = (
                s.execute(
                    select(func.count(Change.id)).where(Change.target_id == t.id)
                ).scalar_one()
                or 0
            )
            last_change_at = s.execute(
                select(Change.created_at)
                .where(Change.target_id == t.id)
                .order_by(Change.id.desc())
                .limit(1)
            ).scalar_one_or_none()

            total_changes += change_count
            if last_change_at and (
                overall_last_change_at is None or last_change_at > overall_last_change_at
            ):
                overall_last_change_at = last_change_at

            target_rows.append(
                {
                    "id": t.id,
                    "name": t.name,
                    "url": t.url,
                    "enabled": t.enabled,
                    "change_count": change_count,
                    "changes_detected": change_count > 0,
                    "last_change_at": _iso_utc(last_change_at),
                    "last_crawl_at": _iso_utc(t.last_crawl_at),
                    "last_status": t.last_status,
                }
            )

    return {
        "changes_detected": total_changes > 0,
        "total_changes": total_changes,
        "last_change_at": _iso_utc(overall_last_change_at),
        "targets": target_rows,
    }
