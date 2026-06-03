"""Background poller that reconciles run state with live Devin sessions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .devin_client import DevinClient, SessionState
from .github_client import GitHubClient
from .models import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    Run,
)
from .reporting import render_summary_md

logger = logging.getLogger("remediation.poller")


def _derive_status(state: SessionState) -> str:
    """Map a Devin session snapshot onto an orchestrator lifecycle status."""
    if not state.is_terminal:
        return STATUS_RUNNING
    verdict = ""
    if isinstance(state.structured_output, dict):
        verdict = str(state.structured_output.get("verdict", "")).lower()
    if state.is_blocked:
        return STATUS_BLOCKED
    if verdict == "pass":
        return STATUS_SUCCEEDED
    if verdict == "fail":
        return STATUS_FAILED
    # Terminal with no explicit verdict: succeeded only if a PR exists.
    return STATUS_SUCCEEDED if state.pr_url else STATUS_FAILED


def reconcile_run(
    run: Run,
    state: SessionState,
    *,
    db: Session,
    github: GitHubClient | None,
) -> bool:
    """Update a run from a session snapshot. Returns True if it just completed."""
    was_terminal = run.status in TERMINAL_STATUSES
    run.status_enum = state.status
    run.status_detail = state.status_detail
    ts = state.raw.get("updated_at")
    if isinstance(ts, (int, float)):
        run.session_updated_at = datetime.fromtimestamp(ts, tz=UTC)
    if state.acus_consumed is not None:
        run.acus_consumed = state.acus_consumed
    if state.pr_url:
        run.pr_url = state.pr_url
    if isinstance(state.structured_output, dict):
        so = state.structured_output
        run.verdict = str(so.get("verdict")) if so.get("verdict") is not None else run.verdict
        run.summary = str(so.get("summary")) if so.get("summary") is not None else run.summary
        if isinstance(so.get("checks"), dict):
            run.checks = so["checks"]
        if not run.pr_url and so.get("pr_url"):
            run.pr_url = str(so["pr_url"])

    run.status = _derive_status(state)
    just_completed = run.status in TERMINAL_STATUSES and not was_terminal
    if just_completed:
        run.completed_at = datetime.now(UTC)
    db.commit()

    if just_completed and github is not None:
        _comment_terminal(run, github)
    return just_completed


def _comment_terminal(run: Run, github: GitHubClient) -> None:
    icon = {STATUS_SUCCEEDED: "✅", STATUS_FAILED: "❌", STATUS_BLOCKED: "⚠️"}.get(run.status, "")
    lines = [f"{icon} Devin remediation **{run.status}** for issue #{run.issue_number}."]
    if run.pr_url:
        lines.append(f"Pull request: {run.pr_url}")
    if run.verdict:
        lines.append(f"Verdict: `{run.verdict}` | checks: `{run.checks or {}}`")
    if run.acus_consumed is not None:
        lines.append(f"ACUs consumed: {run.acus_consumed}")
    if run.summary:
        lines.append(f"Summary: {run.summary}")
    if run.session_url:
        lines.append(f"Session: {run.session_url}")
    try:
        github.comment_issue(run.issue_number, "\n\n".join(lines))
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Failed to post terminal comment for issue #%s", run.issue_number,
                       exc_info=True)


def poll_once(
    *,
    session_factory: sessionmaker,
    devin: DevinClient,
    github: GitHubClient | None,
    settings: Settings,
) -> dict[str, Any]:
    """Poll all active runs exactly once. Returns a small summary dict."""
    completed = 0
    checked = 0
    with session_factory() as db:
        active = db.execute(
            select(Run).where(Run.status == STATUS_RUNNING, Run.session_id.is_not(None))
        ).scalars().all()
        for run in active:
            checked += 1
            try:
                state = devin.get_session(str(run.session_id))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to fetch session %s", run.session_id, exc_info=True)
                continue
            if reconcile_run(run, state, db=db, github=github):
                completed += 1
        try:
            render_summary_md(db, settings.summary_path, repo=settings.repo)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to render summary", exc_info=True)
    return {"checked": checked, "completed": completed}


async def poller_loop(
    *,
    session_factory: sessionmaker,
    devin: DevinClient,
    github: GitHubClient | None,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Run :func:`poll_once` on an interval until ``stop_event`` is set."""
    logger.info("Poller started (interval=%ss)", settings.poll_interval_seconds)
    while not stop_event.is_set():
        try:
            result = await asyncio.to_thread(
                poll_once, session_factory=session_factory, devin=devin, github=github,
                settings=settings,
            )
            if result["checked"]:
                logger.info("Poll: checked=%s completed=%s", result["checked"], result["completed"])
        except Exception:  # noqa: BLE001
            logger.exception("Poller iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval_seconds)
        except TimeoutError:
            pass
    logger.info("Poller stopped")
