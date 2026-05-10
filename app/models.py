from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Target(Base):
    __tablename__ = "target"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    selectors_json: Mapped[str] = mapped_column(Text, default="[]")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    crawls: Mapped[list[Crawl]] = relationship(back_populates="target", cascade="all, delete-orphan")
    changes: Mapped[list[Change]] = relationship(back_populates="target", cascade="all, delete-orphan")

    @property
    def selectors(self) -> list[str]:
        try:
            value = json.loads(self.selectors_json or "[]")
            return [s for s in value if isinstance(s, str) and s.strip()]
        except json.JSONDecodeError:
            return []

    @selectors.setter
    def selectors(self, value: list[str]) -> None:
        self.selectors_json = json.dumps([s.strip() for s in value if s and s.strip()])


class Crawl(Base):
    __tablename__ = "crawl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_selector: Mapped[str | None] = mapped_column(String(500), nullable=True)

    target: Mapped[Target] = relationship(back_populates="crawls")


class Change(Base):
    __tablename__ = "change"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), index=True)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawl.id", ondelete="CASCADE"))
    prev_crawl_id: Mapped[int | None] = mapped_column(ForeignKey("crawl.id", ondelete="SET NULL"), nullable=True)
    unified_diff: Mapped[str] = mapped_column(Text)
    notified_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_email: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    target: Mapped[Target] = relationship(back_populates="changes")


class Settings(Base):
    """Persistent UI-editable settings. Secret credentials live in env vars
    (see :mod:`app.config`) — this table holds operational addresses and the
    global crawl cadence used for every target."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    crawl_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    resend_from: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resend_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
