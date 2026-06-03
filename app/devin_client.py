"""Thin client for the Devin v3 (organization-scoped) REST API.

Only the endpoints needed by the orchestrator are implemented:
  * create a session   -> POST /v3/organizations/{org}/sessions
  * retrieve a session -> GET  /v3/organizations/{org}/sessions/{id}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class CreatedSession:
    """Result of creating a Devin session."""

    session_id: str
    url: str
    status: str | None


@dataclass
class SessionState:
    """A snapshot of a Devin session relevant to remediation tracking."""

    session_id: str
    status: str | None
    status_enum: str | None
    pr_url: str | None
    structured_output: dict[str, Any] | None
    raw: dict[str, Any]

    # status_enum values that mean the session has stopped doing work.
    TERMINAL = {"finished", "expired", "blocked"}

    @property
    def is_terminal(self) -> bool:
        return (self.status_enum or "").lower() in self.TERMINAL


class DevinClient:
    """Minimal Devin v3 API client."""

    def __init__(
        self,
        api_key: str,
        org_id: str,
        base_url: str = "https://api.devin.ai/v3",
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.org_id = org_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = session or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _sessions_url(self) -> str:
        return f"{self.base_url}/organizations/{self.org_id}/sessions"

    def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        idempotent: bool = True,
        create_as_user_id: str | None = None,
    ) -> CreatedSession:
        """Create a new Devin session and return its identifiers."""
        payload: dict[str, Any] = {"prompt": prompt, "idempotent": idempotent}
        if title:
            payload["title"] = title
        if tags:
            payload["tags"] = tags
        if create_as_user_id:
            payload["create_as_user_id"] = create_as_user_id

        resp = self._http.post(
            self._sessions_url(),
            headers=self._headers,
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return CreatedSession(
            session_id=data["session_id"],
            url=data.get("url", ""),
            status=data.get("status"),
        )

    def get_session(self, session_id: str) -> SessionState:
        """Fetch the current state of a session."""
        resp = self._http.get(
            f"{self._sessions_url()}/{session_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        pr = data.get("pull_request") or {}
        return SessionState(
            session_id=data.get("session_id", session_id),
            status=data.get("status"),
            status_enum=data.get("status_enum"),
            pr_url=pr.get("url") if isinstance(pr, dict) else None,
            structured_output=data.get("structured_output"),
            raw=data,
        )
