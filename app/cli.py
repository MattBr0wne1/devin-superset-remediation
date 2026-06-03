"""Command-line entry point for ops and manual/automated replay.

Subcommands:
  dispatch --issue N   Fetch issue N and start a remediation session.
  poll [--loop]        Reconcile active runs once (or continuously).
  report               Regenerate the Markdown summary and print metrics.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import get_settings
from .dispatcher import handle_labeled_issue
from .github_client import GitHubClient
from .main import build_clients
from .models import make_session_factory
from .poller import poll_once
from .reporting import compute_metrics, render_summary_md

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _cmd_dispatch(issue_number: int, force: bool = False) -> int:
    settings = get_settings()
    session_factory = make_session_factory(settings.database_url)
    devin, github = build_clients(settings)
    if github is None:
        github = GitHubClient(settings.github_token, settings.repo)
    issue = github.get_issue(issue_number)
    with session_factory() as db:
        run = handle_labeled_issue(issue, db=db, devin=devin, github=github,
                                   settings=settings, force=force)
        print(json.dumps(run.to_dict(), indent=2))
    return 0


def _cmd_poll(loop: bool) -> int:
    settings = get_settings()
    session_factory = make_session_factory(settings.database_url)
    devin, github = build_clients(settings)
    while True:
        result = poll_once(session_factory=session_factory, devin=devin, github=github,
                           settings=settings)
        print(json.dumps(result))
        if not loop:
            return 0
        time.sleep(settings.poll_interval_seconds)


def _cmd_report() -> int:
    settings = get_settings()
    session_factory = make_session_factory(settings.database_url)
    with session_factory() as db:
        render_summary_md(db, settings.summary_path, repo=settings.repo)
        print(json.dumps(compute_metrics(db), indent=2))
    print(f"Wrote {settings.summary_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="remediation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dispatch = sub.add_parser("dispatch", help="Start a remediation session for an issue")
    p_dispatch.add_argument("--issue", type=int, required=True)
    p_dispatch.add_argument("--force", action="store_true",
                            help="Re-trigger even if a run already exists for this issue")

    p_poll = sub.add_parser("poll", help="Reconcile active runs with live sessions")
    p_poll.add_argument("--loop", action="store_true")

    sub.add_parser("report", help="Regenerate the summary report")

    args = parser.parse_args(argv)
    if args.command == "dispatch":
        return _cmd_dispatch(args.issue, args.force)
    if args.command == "poll":
        return _cmd_poll(args.loop)
    if args.command == "report":
        return _cmd_report()
    return 1


if __name__ == "__main__":
    sys.exit(main())
