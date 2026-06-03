"""Tests for poller reconciliation and verdict mapping."""

from __future__ import annotations

from app.dispatcher import handle_labeled_issue
from app.models import STATUS_FAILED, STATUS_RUNNING, STATUS_SUCCEEDED, Run
from app.poller import poll_once
from tests.conftest import make_issue


def _dispatch(settings, session_factory, fake_devin, fake_github, number):
    with session_factory() as db:
        return handle_labeled_issue(make_issue(number=number), db=db, devin=fake_devin,
                                    github=fake_github, settings=settings).session_id


def test_poll_marks_success_on_pass_verdict(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 1)
    fake_devin.set_state(sid, status_enum="finished", pr_url="https://github.com/x/y/pull/1",
                         structured_output={"verdict": "pass",
                                            "checks": {"precommit": True, "tests": True},
                                            "summary": "Replaced utcnow"})
    result = poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
                       settings=settings)
    assert result == {"checked": 1, "completed": 1}
    with session_factory() as db:
        run = db.query(Run).one()
        assert run.status == STATUS_SUCCEEDED
        assert run.verdict == "pass"
        assert run.pr_url.endswith("/pull/1")
        assert run.completed_at is not None
        assert run.checks == {"precommit": True, "tests": True}
    # terminal comment posted
    assert any("succeeded" in c[1] for c in fake_github.comments)


def test_poll_marks_failed_on_fail_verdict(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 2)
    fake_devin.set_state(sid, status_enum="finished",
                         structured_output={"verdict": "fail", "summary": "could not fix"})
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    with session_factory() as db:
        assert db.query(Run).one().status == STATUS_FAILED


def test_poll_keeps_running_when_not_terminal(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 3)
    fake_devin.set_state(sid, status_enum="working")
    result = poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
                       settings=settings)
    assert result["completed"] == 0
    with session_factory() as db:
        assert db.query(Run).one().status == STATUS_RUNNING
