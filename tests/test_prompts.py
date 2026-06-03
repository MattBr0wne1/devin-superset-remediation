"""Tests for the remediation prompt contract.

These lock in the constraints the prompt must always carry so that sessions
produce PRs that pass Apache Superset's CI (Conventional Commits title, ASF
license headers) and keep the orchestrator's observability honest (progress
heartbeat, machine-readable verdict).
"""

from __future__ import annotations

from app.prompts import build_remediation_prompt

ISSUE = {"number": 42, "title": "Fix the thing", "body": "Details about the thing."}
REPO = "owner/repo"


def test_prompt_includes_issue_and_branch() -> None:
    prompt = build_remediation_prompt(ISSUE, REPO)
    assert "#42" in prompt
    assert "Fix the thing" in prompt
    assert "Details about the thing." in prompt
    assert "devin/fix-issue-42" in prompt
    assert "Fixes #42" in prompt


def test_prompt_requires_conventional_commit_pr_title() -> None:
    prompt = build_remediation_prompt(ISSUE, REPO)
    assert "Conventional Commits" in prompt
    assert "type(scope): description" in prompt


def test_prompt_requires_asf_header_for_new_files() -> None:
    prompt = build_remediation_prompt(ISSUE, REPO)
    assert "Apache Software" in prompt
    assert "license header" in prompt.lower()


def test_prompt_asks_for_progress_heartbeat() -> None:
    prompt = build_remediation_prompt(ISSUE, REPO)
    assert "progress message" in prompt.lower()
    assert "heartbeat" in prompt.lower()


def test_prompt_keeps_scoped_precommit_and_verdict() -> None:
    prompt = build_remediation_prompt(ISSUE, REPO)
    assert "--files" in prompt
    assert "--all-files" in prompt  # mentioned as the thing NOT to do
    assert "structured_output" in prompt
    assert '"verdict"' in prompt


def test_prompt_respects_draft_flag() -> None:
    assert "DRAFT pull request" in build_remediation_prompt(ISSUE, REPO, draft_pr=True)
    draft_false = build_remediation_prompt(ISSUE, REPO, draft_pr=False)
    assert "DRAFT" not in draft_false
    assert "a pull request" in draft_false


def test_prompt_handles_empty_body() -> None:
    prompt = build_remediation_prompt({"number": 1, "title": "t", "body": None}, REPO)
    assert "(no description provided)" in prompt
