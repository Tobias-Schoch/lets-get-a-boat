from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from .config import config
from .models import Base, Change, Crawl, Settings, Target

logger = logging.getLogger(__name__)

KEEP_PER_TARGET = 24  # crawls + their changes retained per target after each crawl

SEED_TARGETS: list[dict] = [
    {
        "name": "Konstanz – Bootsliegeplatz (Bauen + Wohnen)",
        "url": "https://www.konstanz.de/stadt+gestalten/bauen+_+wohnen/privat+bauen/bootsliegeplatz",
        "selectors": ["section#content", "article.composedcontent-standardseite-konstanz"],
    },
    {
        "name": "Konstanz – Pressemitteilung Seerhein",
        "url": "https://www.konstanz.de/service/presse/pressemitteilungen/bootsliegeplaetze+am+seerhein",
        "selectors": ["section#content", "article.composedcontent-pressemeldung"],
    },
    {
        # JSON-API behind the service-bw.de SPA — content is returned as
        # application/json. The extractor auto-detects JSON and pretty-prints
        # with sorted keys, so we don't need a CSS selector here.
        "name": "Service BW – Liegeplatz (JSON-API)",
        "url": "https://www.service-bw.de/rest/api/leistungen/6001501?ags=08335043",
        "selectors": [],
    },
]


def _make_engine():
    db_path = Path(config.data_dir) / "boat.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )


engine = _make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _migrate(conn) -> None:
    """Tiny in-place schema migrations for columns added after first deploy.

    We could pull in alembic, but for two-or-three additive changes a PRAGMA
    check is plenty.
    """
    cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(settings)").fetchall()}
    if "crawl_interval_minutes" not in cols:
        conn.exec_driver_sql(
            "ALTER TABLE settings ADD COLUMN crawl_interval_minutes INTEGER NOT NULL DEFAULT 30"
        )
    if "test_suffix" not in cols:
        conn.exec_driver_sql(
            "ALTER TABLE settings ADD COLUMN test_suffix VARCHAR(500) DEFAULT ''"
        )


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate(conn)
    with session_scope() as s:
        if s.execute(select(Settings).where(Settings.id == 1)).scalar_one_or_none() is None:
            s.add(Settings(id=1))
        existing_urls = {row[0] for row in s.execute(select(Target.url)).all()}
        for seed in SEED_TARGETS:
            if seed["url"] in existing_urls:
                continue
            t = Target(name=seed["name"], url=seed["url"], interval_minutes=30, enabled=True)
            t.selectors = seed["selectors"]
            s.add(t)


def get_settings(s: Session) -> Settings:
    obj = s.execute(select(Settings).where(Settings.id == 1)).scalar_one_or_none()
    if obj is None:
        obj = Settings(id=1)
        s.add(obj)
        s.flush()
    return obj


def prune_target_history(target_id: int, keep: int = KEEP_PER_TARGET) -> tuple[int, int]:
    """Trim crawls + changes for one target to the latest ``keep`` rows.

    Called at the end of every crawl so the DB stays bounded. Returns the
    number of (crawls, changes) deleted, mostly for logging.
    """
    with session_scope() as s:
        keep_crawl_ids = (
            s.execute(
                select(Crawl.id)
                .where(Crawl.target_id == target_id)
                .order_by(Crawl.id.desc())
                .limit(keep)
            )
            .scalars()
            .all()
        )
        if not keep_crawl_ids:
            return (0, 0)
        # Drop dependent changes first so we don't leave dangling crawl_id FKs.
        ch_result = s.execute(
            delete(Change).where(
                Change.target_id == target_id,
                Change.crawl_id.notin_(keep_crawl_ids),
            )
        )
        cr_result = s.execute(
            delete(Crawl).where(
                Crawl.target_id == target_id,
                Crawl.id.notin_(keep_crawl_ids),
            )
        )
        return (cr_result.rowcount or 0, ch_result.rowcount or 0)
