"""SQLite persistence for remediation runs (the observability store)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# Orchestrator-level lifecycle status (distinct from Devin's status_enum).
STATUS_DISPATCHED = "dispatched"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

TERMINAL_STATUSES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_BLOCKED}


class Run(Base):
    """A single remediation attempt for one GitHub issue."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    issue_title: Mapped[str] = mapped_column(String(512), default="")

    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=STATUS_DISPATCHED, index=True)
    status_enum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    checks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def checks(self) -> dict[str, Any]:
        return json.loads(self.checks_json) if self.checks_json else {}

    @checks.setter
    def checks(self, value: dict[str, Any]) -> None:
        self.checks_json = json.dumps(value)

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.created_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "session_id": self.session_id,
            "session_url": self.session_url,
            "status": self.status,
            "status_enum": self.status_enum,
            "verdict": self.verdict,
            "pr_url": self.pr_url,
            "checks": self.checks,
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
        }


def make_session_factory(database_url: str) -> sessionmaker:
    """Create the engine + schema and return a session factory."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
