"""Tests for poller reconciliation and verdict mapping."""

from __future__ import annotations

from app.devin_client import _first_pr_url
from app.dispatcher import handle_labeled_issue
from app.models import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_PR_OPEN,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    Run,
)
from app.poller import poll_once
from tests.conftest import make_issue


def test_first_pr_url_reads_v3_pr_url_key():
    # Real v3 shape: list of {"pr_url": ..., "pr_state": ...}.
    assert _first_pr_url([{"pr_url": "https://x/pull/4", "pr_state": "open"}]) == "https://x/pull/4"
    assert _first_pr_url([{"url": "https://x/pull/5"}]) == "https://x/pull/5"
    assert _first_pr_url([]) is None
    assert _first_pr_url(None) is None


def _dispatch(settings, session_factory, fake_devin, fake_github, number):
    with session_factory() as db:
        return handle_labeled_issue(make_issue(number=number), db=db, devin=fake_devin,
                                    github=fake_github, settings=settings).session_id


def test_poll_marks_success_on_pass_verdict(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 1)
    # "exit" is the real v3 terminal status value.
    fake_devin.set_state(sid, status="exit", status_detail="finished",
                         pr_url="https://github.com/x/y/pull/1",
                         acus_consumed=3.5, raw={"updated_at": 1700000000},
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
        assert run.acus_consumed == 3.5
        assert run.status_detail == "finished"
        assert run.session_updated_at is not None
    # terminal comment posted
    assert any("succeeded" in c[1] for c in fake_github.comments)


def test_poll_terminal_on_finished_detail(settings, session_factory, fake_devin, fake_github):
    # status still "running" but status_detail "finished" means the task is done.
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 5)
    fake_devin.set_state(sid, status="running", status_detail="finished",
                         pr_url="https://github.com/x/y/pull/9")
    result = poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
                       settings=settings)
    assert result == {"checked": 1, "completed": 1}
    with session_factory() as db:
        assert db.query(Run).one().status == STATUS_SUCCEEDED


def test_poll_marks_failed_on_fail_verdict(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 2)
    fake_devin.set_state(sid, status="finished",
                         structured_output={"verdict": "fail", "summary": "could not fix"})
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    with session_factory() as db:
        assert db.query(Run).one().status == STATUS_FAILED


def test_poll_keeps_running_when_not_terminal(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 3)
    fake_devin.set_state(sid, status="running", status_detail="working",
                         raw={"updated_at": 1700000000})
    result = poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
                       settings=settings)
    assert result["completed"] == 0
    with session_factory() as db:
        run = db.query(Run).one()
        assert run.status == STATUS_RUNNING
        # live detail + heartbeat captured even while still running
        assert run.status_detail == "working"
        assert run.session_updated_at is not None


def test_poll_discovers_pr_by_branch_when_session_lags(
    settings, session_factory, fake_devin, fake_github
):
    # Session is still running with no pr_url, but a PR already exists on GitHub.
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 7)
    fake_github.prs_by_branch["devin/fix-issue-7"] = "https://github.com/x/y/pull/42"
    fake_devin.set_state(sid, status="running", status_detail="waiting_for_user")
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    with session_factory() as db:
        run = db.query(Run).one()
        # A raised PR flips the run to the clear "PR open / awaiting review" state,
        # not the ambiguous raw "waiting_for_user".
        assert run.status == STATUS_PR_OPEN
        assert run.pr_url == "https://github.com/x/y/pull/42"
    # a one-time "PR raised, awaiting review" comment is posted
    assert any("awaiting human review" in c[1] for c in fake_github.comments)


def test_poll_nudges_stalled_session_once(settings, session_factory, fake_devin, fake_github):
    import time

    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 9)
    # Running, no PR, heartbeat older than the stall threshold -> should be nudged.
    stale = int(time.time()) - (settings.stall_seconds + 120)
    fake_devin.set_state(sid, status="running", status_detail="working",
                         raw={"updated_at": stale})
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    assert len(fake_devin.messages) == 1
    assert fake_devin.messages[0][0] == sid
    with session_factory() as db:
        assert db.query(Run).one().nudged_at is not None
    # A second poll while still stalled must NOT nudge again (one-shot).
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    assert len(fake_devin.messages) == 1


def test_poll_does_not_nudge_fresh_session(settings, session_factory, fake_devin, fake_github):
    import time

    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 10)
    fake_devin.set_state(sid, status="running", status_detail="working",
                         raw={"updated_at": int(time.time())})
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    assert fake_devin.messages == []


def test_poll_blocked_on_suspended(settings, session_factory, fake_devin, fake_github):
    sid = _dispatch(settings, session_factory, fake_devin, fake_github, 6)
    fake_devin.set_state(sid, status="suspended", status_detail="out_of_credits")
    poll_once(session_factory=session_factory, devin=fake_devin, github=fake_github,
              settings=settings)
    with session_factory() as db:
        assert db.query(Run).one().status == STATUS_BLOCKED
