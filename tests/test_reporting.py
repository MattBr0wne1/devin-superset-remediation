"""Tests for metrics aggregation and summary rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import STATUS_FAILED, STATUS_SUCCEEDED, Run
from app.reporting import compute_metrics, render_summary_md


def _seed(session_factory):
    now = datetime.now(UTC)
    with session_factory() as db:
        r1 = Run(issue_number=1, issue_title="a", status=STATUS_SUCCEEDED, verdict="pass",
                 pr_url="https://x/pull/1", session_url="https://s/1")
        r1.completed_at = now + timedelta(seconds=120)
        r2 = Run(issue_number=2, issue_title="b", status=STATUS_FAILED, verdict="fail")
        r2.completed_at = now + timedelta(seconds=60)
        r3 = Run(issue_number=3, issue_title="c", status="running")
        db.add_all([r1, r2, r3])
        db.commit()


def test_compute_metrics(settings, session_factory):
    _seed(session_factory)
    with session_factory() as db:
        m = compute_metrics(db)
    assert m["total_runs"] == 3
    assert m["succeeded"] == 1
    assert m["failed"] == 1
    assert m["in_flight"] == 1
    assert m["terminal"] == 2
    assert m["success_rate"] == 0.5
    assert m["pr_count"] == 1


def test_render_summary_md(settings, session_factory, tmp_path):
    _seed(session_factory)
    path = str(tmp_path / "summary.md")
    with session_factory() as db:
        text = render_summary_md(db, path, repo="MattBr0wne1/superset")
    assert "# Remediation Report" in text
    assert "Remediation rate" in text and "Verified pass" in text
    assert "#1" in text and "#2" in text
    with open(path) as fh:
        assert fh.read() == text
