"""Simulate the full remediation workflow with NO credentials.

Uses in-memory fake Devin/GitHub clients to drive the real dispatcher, poller,
and reporting code end to end, then prints metrics and writes a summary report.
Useful for reviewers who want to see the pipeline without a Devin API key.

    python -m scripts.demo
"""

from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.devin_client import CreatedSession, SessionState
from app.dispatcher import handle_labeled_issue
from app.models import make_session_factory
from app.poller import poll_once
from app.reporting import compute_metrics, render_summary_md


class DemoDevin:
    """Fake Devin client that 'completes' sessions with scripted verdicts."""

    def __init__(self, scripted: dict[int, dict[str, Any]]) -> None:
        self._scripted = scripted  # issue_number -> structured_output
        self._sid_to_issue: dict[str, int] = {}
        self._n = 0

    def create_session(self, prompt: str, **kwargs: Any) -> CreatedSession:
        self._n += 1
        sid = f"devin-demo-{self._n}"
        # tags include issue-<n>; recover the issue number for scripting
        for tag in kwargs.get("tags", []):
            if tag.startswith("issue-"):
                self._sid_to_issue[sid] = int(tag.split("-", 1)[1])
        return CreatedSession(sid, f"https://app.devin.ai/sessions/{sid}", "running")

    def get_session(self, session_id: str) -> SessionState:
        issue = self._sid_to_issue.get(session_id)
        so = self._scripted.get(issue, {"verdict": "pass", "checks": {"precommit": True,
                                                                      "tests": True}})
        pr = so.get("pr_url") or (f"https://github.com/MattBr0wne1/superset/pull/{issue}"
                                  if so.get("verdict") == "pass" else None)
        acus = float(so.get("acus_consumed", 4.0))
        return SessionState(session_id, "finished", "finished", pr, acus, so, {})


class DemoGitHub:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []

    def comment_issue(self, number: int, body: str) -> dict[str, Any]:
        self.comments.append((number, body))
        return {}

    def find_pr_by_branch(self, branch: str) -> str | None:
        # The session object already surfaces the PR in this demo, so the
        # branch-lookup fallback is never needed.
        return None


def main() -> int:
    settings = Settings(db_path="demo.db", summary_path="summary.md",
                        repo="MattBr0wne1/superset")
    # Fresh DB for the demo
    import os
    if os.path.exists("demo.db"):
        os.remove("demo.db")
    factory = make_session_factory(settings.database_url)

    scripted = {
        101: {"verdict": "pass", "checks": {"precommit": True, "tests": True},
              "summary": "Replaced datetime.utcnow() in dates.py"},
        102: {"verdict": "pass", "checks": {"precommit": True, "tests": True},
              "summary": "Replaced datetime.utcnow() in cache.py"},
        103: {"verdict": "fail", "checks": {"precommit": False, "tests": False},
              "summary": "Could not satisfy pre-commit"},
    }
    devin = DemoDevin(scripted)
    github = DemoGitHub()

    issues = [
        {"number": 101, "title": "Replace utcnow in dates.py", "body": "..."},
        {"number": 102, "title": "Replace utcnow in cache.py", "body": "..."},
        {"number": 103, "title": "Replace utcnow in log_prune.py", "body": "..."},
    ]
    print("Dispatching", len(issues), "labeled issues...")
    for issue in issues:
        with factory() as db:
            handle_labeled_issue(issue, db=db, devin=devin, github=github, settings=settings)

    print("Polling sessions to completion...")
    poll_once(session_factory=factory, devin=devin, github=github, settings=settings)

    with factory() as db:
        metrics = compute_metrics(db)
        render_summary_md(db, settings.summary_path, repo=settings.repo)
    print(json.dumps(metrics, indent=2))
    print(f"\nWrote {settings.summary_path}. Issue comments posted: {len(github.comments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
