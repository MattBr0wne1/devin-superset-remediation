"""Core dispatch logic: turn a labeled issue into a managed Devin session.

This is the single entry point shared by every trigger (FastAPI webhook, GitHub
Actions CLI, manual replay). Keeping it trigger-agnostic is what lets new event
sources — e.g. a Snyk or Dependabot scan — "slot into the same handler".
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .devin_client import DevinClient
from .github_client import GitHubClient
from .models import STATUS_RUNNING, Run
from .prompts import build_remediation_prompt, verdict_json_schema

logger = logging.getLogger("remediation.dispatcher")


def get_run_for_issue(db: Session, issue_number: int) -> Run | None:
    return db.execute(select(Run).where(Run.issue_number == issue_number)).scalar_one_or_none()


def handle_labeled_issue(
    issue: dict[str, Any],
    *,
    db: Session,
    devin: DevinClient,
    github: GitHubClient | None,
    settings: Settings,
) -> Run:
    """Create (or return an existing) remediation run for a labeled issue.

    Idempotent: a second label event for the same issue returns the existing run
    instead of spawning a duplicate session.
    """
    number = int(issue["number"])
    title = issue.get("title", "")

    existing = get_run_for_issue(db, number)
    if existing is not None:
        logger.info("Issue #%s already has run %s; skipping duplicate", number, existing.id)
        return existing

    run = Run(issue_number=number, issue_title=title)
    db.add(run)
    db.flush()  # assign id before external calls

    prompt = build_remediation_prompt(issue, settings.repo, draft_pr=settings.draft_pr)
    created = devin.create_session(
        prompt,
        title=f"Remediate {settings.repo}#{number}: {title}"[:120],
        tags=["remediation", "superset", f"issue-{number}"],
        idempotent=True,
        create_as_user_id=settings.create_as_user_id,
        structured_output_schema=verdict_json_schema(),
        max_acu_limit=settings.max_acu_limit,
    )

    run.session_id = created.session_id
    run.session_url = created.url
    run.status = STATUS_RUNNING
    run.status_enum = created.status
    db.commit()

    logger.info("Dispatched issue #%s -> session %s (%s)", number, created.session_id, created.url)

    if github is not None:
        try:
            github.comment_issue(
                number,
                f"Devin remediation started — tracking session: {created.url}\n\n"
                f"This run will open a draft PR once the fix is verified.",
            )
        except Exception:  # noqa: BLE001 - commenting is best-effort
            logger.warning("Failed to comment dispatch notice on issue #%s", number, exc_info=True)

    return run
