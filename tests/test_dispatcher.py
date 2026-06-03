"""Tests for the dispatch logic (idempotency, session creation, commenting)."""

from __future__ import annotations

from app.dispatcher import handle_labeled_issue
from app.models import STATUS_RUNNING
from tests.conftest import make_issue


def test_dispatch_creates_session_and_run(settings, session_factory, fake_devin, fake_github):
    issue = make_issue(number=42)
    with session_factory() as db:
        run = handle_labeled_issue(issue, db=db, devin=fake_devin, github=fake_github,
                                   settings=settings)
        assert run.issue_number == 42
        assert run.status == STATUS_RUNNING
        assert run.session_id == "devin-1"
        assert run.session_url.endswith("devin-1")

    assert len(fake_devin.created) == 1
    assert "#42" in fake_devin.created[0]["prompt"] or "42" in fake_devin.created[0]["prompt"]
    assert fake_github.comments and fake_github.comments[0][0] == 42


def test_dispatch_is_idempotent(settings, session_factory, fake_devin, fake_github):
    issue = make_issue(number=7)
    with session_factory() as db:
        run1 = handle_labeled_issue(issue, db=db, devin=fake_devin, github=fake_github,
                                    settings=settings)
    with session_factory() as db:
        run2 = handle_labeled_issue(issue, db=db, devin=fake_devin, github=fake_github,
                                    settings=settings)
    assert run1.id == run2.id
    assert len(fake_devin.created) == 1  # no duplicate session


def test_dispatch_force_retriggers(settings, session_factory, fake_devin, fake_github):
    issue = make_issue(number=7)
    with session_factory() as db:
        run1 = handle_labeled_issue(issue, db=db, devin=fake_devin, github=fake_github,
                                    settings=settings)
        assert run1.session_id == "devin-1"
    with session_factory() as db:
        run2 = handle_labeled_issue(issue, db=db, devin=fake_devin, github=fake_github,
                                    settings=settings, force=True)
        assert run2.session_id == "devin-2"  # a fresh session replaced the old run
    assert len(fake_devin.created) == 2


def test_dispatch_without_github(settings, session_factory, fake_devin):
    issue = make_issue(number=9)
    with session_factory() as db:
        run = handle_labeled_issue(issue, db=db, devin=fake_devin, github=None, settings=settings)
        assert run.session_id == "devin-1"
