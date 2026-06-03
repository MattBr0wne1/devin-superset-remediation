"""Minimal GitHub REST API client for the orchestrator.

Used to author issues, post status comments, manage the trigger label, and
(optionally) provision the repo's Issues tab and webhook. Authentication uses a
token supplied via configuration.
"""

from __future__ import annotations

from typing import Any

import requests

GITHUB_API = "https://api.github.com"


class GitHubClient:
    """Small wrapper over the GitHub REST API scoped to a single repo."""

    def __init__(
        self,
        token: str,
        repo: str,
        api_url: str = GITHUB_API,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.token = token
        self.repo = repo  # "owner/name"
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._http = session or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    def _repo_url(self, *parts: str) -> str:
        return "/".join([f"{self.api_url}/repos/{self.repo}", *parts])

    # --- issues ---
    def get_issue(self, number: int) -> dict[str, Any]:
        resp = self._http.get(self._repo_url("issues", str(number)), headers=self._headers,
                              timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def create_issue(self, title: str, body: str,
                     labels: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        resp = self._http.post(self._repo_url("issues"), headers=self._headers, json=payload,
                               timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def comment_issue(self, number: int, body: str) -> dict[str, Any]:
        resp = self._http.post(self._repo_url("issues", str(number), "comments"),
                               headers=self._headers, json={"body": body}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def add_labels(self, number: int, labels: list[str]) -> dict[str, Any]:
        resp = self._http.post(self._repo_url("issues", str(number), "labels"),
                               headers=self._headers, json={"labels": labels}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # --- pull requests ---
    def find_pr_by_branch(self, branch: str) -> str | None:
        """Return the html_url of the PR opened from ``branch``, if any.

        Lets the poller surface a PR as soon as it exists on GitHub, without
        waiting for the Devin session object's ``pull_requests`` array to catch
        up (which can lag or stay empty while the session waits on a human).
        """
        resp = self._http.get(
            self._repo_url("pulls"),
            headers=self._headers,
            params={"head": f"{self.owner}:{branch}", "state": "all", "per_page": "1"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        items = resp.json()
        if items:
            return str(items[0].get("html_url")) or None
        return None

    # --- repo administration ---
    def ensure_label(self, name: str, color: str = "5319e7", description: str = "") -> None:
        """Create the label if it does not already exist (idempotent)."""
        resp = self._http.post(self._repo_url("labels"), headers=self._headers,
                               json={"name": name, "color": color, "description": description},
                               timeout=self.timeout)
        if resp.status_code in (200, 201):
            return
        if resp.status_code == 422:  # already exists
            return
        resp.raise_for_status()

    def enable_issues(self) -> dict[str, Any]:
        """Turn on the repo's Issues tab (requires administration rights)."""
        resp = self._http.patch(f"{self.api_url}/repos/{self.repo}", headers=self._headers,
                                json={"has_issues": True}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def create_repo_for_authenticated_user(
        self, name: str, *, private: bool = False, description: str = ""
    ) -> dict[str, Any]:
        resp = self._http.post(f"{self.api_url}/user/repos", headers=self._headers,
                               json={"name": name, "private": private, "description": description,
                                     "has_issues": True}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def create_webhook(self, callback_url: str, secret: str,
                       events: list[str] | None = None) -> dict[str, Any]:
        """Register a webhook on the repo (requires admin:repo_hook)."""
        payload = {
            "name": "web",
            "active": True,
            "events": events or ["issues"],
            "config": {"url": callback_url, "content_type": "json", "secret": secret},
        }
        resp = self._http.post(self._repo_url("hooks"), headers=self._headers, json=payload,
                               timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
