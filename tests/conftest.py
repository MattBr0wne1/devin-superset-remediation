"""Shared test fixtures and in-memory fakes (no network calls)."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.devin_client import CreatedSession, SessionState
from app.models import make_session_factory


class FakeDevin:
    """Stand-in for DevinClient that records calls and returns canned state."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self._states: dict[str, SessionState] = {}
        self._counter = 0

    def create_session(self, prompt: str, **kwargs: Any) -> CreatedSession:
        self._counter += 1
        sid = f"devin-{self._counter}"
        self.created.append({"prompt": prompt, "session_id": sid, **kwargs})
        return CreatedSession(session_id=sid, url=f"https://app.devin.ai/sessions/{sid}",
                              status="running")

    def set_state(self, session_id: str, **kwargs: Any) -> None:
        self._states[session_id] = SessionState(
            session_id=session_id,
            status=kwargs.get("status"),
            status_detail=kwargs.get("status_detail"),
            pr_url=kwargs.get("pr_url"),
            acus_consumed=kwargs.get("acus_consumed"),
            structured_output=kwargs.get("structured_output"),
            raw=kwargs.get("raw", {}),
        )

    def get_session(self, session_id: str) -> SessionState:
        return self._states[session_id]


class FakeGitHub:
    """Stand-in for GitHubClient that records side effects."""

    def __init__(self, issues: dict[int, dict[str, Any]] | None = None) -> None:
        self.issues = issues or {}
        self.comments: list[tuple[int, str]] = []
        self.created_issues: list[dict[str, Any]] = []
        self.labels: list[str] = []

    def get_issue(self, number: int) -> dict[str, Any]:
        return self.issues[number]

    def comment_issue(self, number: int, body: str) -> dict[str, Any]:
        self.comments.append((number, body))
        return {"id": len(self.comments)}

    def ensure_label(self, name: str, color: str = "", description: str = "") -> None:
        self.labels.append(name)

    def create_issue(self, title: str, body: str,
                     labels: list[str] | None = None) -> dict[str, Any]:
        n = len(self.created_issues) + 1000
        rec = {"number": n, "title": title, "html_url": f"https://x/{n}"}
        self.created_issues.append(rec)
        return rec


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        devin_api_key="cog_test",
        devin_org_id="org_test",
        github_token="",
        repo="MattBr0wne1/superset",
        trigger_label="devin-fix",
        webhook_secret="",
        db_path=str(tmp_path / "test.db"),
        summary_path=str(tmp_path / "summary.md"),
    )


@pytest.fixture
def session_factory(settings: Settings) -> Any:
    return make_session_factory(settings.database_url)


@pytest.fixture
def fake_devin() -> FakeDevin:
    return FakeDevin()


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


def make_issue(number: int = 1, title: str = "Fix utcnow", body: str = "do it") -> dict[str, Any]:
    return {"number": number, "title": title, "body": body}
