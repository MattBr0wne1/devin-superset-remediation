"""Prompt construction for remediation sessions.

The prompt is the contract between the orchestrator and Devin: it tells the
session what to fix, how to verify, and — crucially — to return a
machine-readable verdict in ``structured_output`` so the poller can record an
objective pass/fail signal.
"""

from __future__ import annotations

from typing import Any


def build_remediation_prompt(issue: dict[str, Any], repo: str, *, draft_pr: bool = True) -> str:
    """Build the instruction prompt for a single issue."""
    number = issue["number"]
    title = issue.get("title", "")
    body = (issue.get("body") or "").strip()
    draft = "a DRAFT pull request" if draft_pr else "a pull request"

    return f"""You are an automated remediation agent working on the GitHub repository \
`{repo}`.

Remediate GitHub issue #{number}: "{title}".

Issue description:
---
{body or "(no description provided)"}
---

Requirements:
1. Clone/checkout `{repo}` and create a new branch named `devin/fix-issue-{number}`.
2. Implement the minimal, focused change that resolves the issue. Do not make
   unrelated changes.
   - If you add any NEW source file, prepend the standard Apache Software
     Foundation (ASF) license header — this repo's license check (RAT) fails any
     new file without it.
3. Verify your change before opening a PR:
   - Stage your edits (`git add`) and run pre-commit on ONLY the files you changed:
     `pre-commit run --files <your changed files>` and make it pass (formatting,
     ruff, mypy). Do NOT run `--all-files` — on this repo it scans thousands of
     files and can take far too long; the changed-file run validates the same hooks
     for your edit.
   - Run the tests relevant to the files you touched and make them pass.
   - Do not open a PR until verification is green; if you cannot get it green,
     report verdict "fail" with the reason.
   - Work continuously to completion — do not pause to ask for confirmation.
   - Post a brief one-line progress message at each milestone (e.g. "cloned",
     "fix applied", "pre-commit green", "PR opened"). This keeps the session's
     heartbeat fresh so the orchestrator can tell you are progressing rather than
     stalled.
4. Open {draft} into `{repo}`. The PR TITLE must follow Conventional Commits —
   `type(scope): description` (types: fix, feat, docs, refactor, perf, test,
   chore) — because this repo enforces it in CI. The PR description must include
   "Fixes #{number}".
5. When finished, set the session's `structured_output` to EXACTLY this JSON shape:
   {{
     "verdict": "pass" or "fail",
     "pr_url": "<the pull request URL, or empty string if none>",
     "checks": {{"precommit": true/false, "tests": true/false}},
     "summary": "<one sentence describing what you changed>"
   }}

The `structured_output` verdict is consumed by an automated pipeline, so it must
be valid and accurate.
"""
