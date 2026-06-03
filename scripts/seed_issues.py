"""Author the seed remediation issues (and trigger label) on the fork.

Run once after the fork's Issues tab is enabled:

    python -m scripts.seed_issues               # create label + issues
    python -m scripts.seed_issues --label-only  # just ensure the label
    python -m scripts.seed_issues --dry-run     # print what would be created

Each issue is intentionally small, self-contained, and verifiable via
`pre-commit run --all-files`, so a Devin session can fix and prove it green.
"""

from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.github_client import GitHubClient

TRIGGER_LABEL = "devin-fix"
LABEL_COLOR = "5319e7"
LABEL_DESC = "Remediated automatically by Devin"

ISSUES: list[dict[str, object]] = [
    {
        "title": "chore(utils): replace deprecated datetime.utcnow() in superset/utils/dates.py",
        "body": (
            "### Problem\n"
            "`superset/utils/dates.py` calls `datetime.utcnow()`, which is deprecated in "
            "Python 3.12 and returns a **naive** datetime. It is then passed to "
            "`datetime_to_epoch()`, which does `dttm.astimezone(pytz.utc)`; calling "
            "`astimezone` on a naive datetime assumes the *system local* timezone, so the "
            "epoch value is wrong on any non-UTC host.\n\n"
            "### Fix\n"
            "Use a timezone-aware UTC datetime: replace `datetime.utcnow()` with "
            "`datetime.now(timezone.utc)` (import `timezone` from `datetime`).\n\n"
            "### Acceptance criteria\n"
            "- No `datetime.utcnow()` remains in `superset/utils/dates.py`.\n"
            "- `pre-commit run --all-files` passes (ruff, mypy).\n"
            "- Existing tests for date utils still pass."
        ),
    },
    {
        "title": "chore(utils): replace deprecated datetime.utcnow() in superset/utils/cache.py",
        "body": (
            "### Problem\n"
            "`superset/utils/cache.py` uses `datetime.utcnow()` in two places "
            "(cache-key timestamp and `content_changed_time`). `datetime.utcnow()` is "
            "deprecated in Python 3.12.\n\n"
            "### Fix\n"
            "Replace both with `datetime.now(timezone.utc)` and adjust the isoformat "
            "slicing so behaviour is unchanged.\n\n"
            "### Acceptance criteria\n"
            "- No `datetime.utcnow()` remains in `superset/utils/cache.py`.\n"
            "- `pre-commit run --all-files` passes.\n"
            "- Cache-related unit tests still pass."
        ),
    },
    {
        "title": "chore(reports): replace deprecated datetime.utcnow() in report log pruning",
        "body": (
            "### Problem\n"
            "`superset/commands/report/log_prune.py` computes `from_date` with the "
            "deprecated `datetime.utcnow()`.\n\n"
            "### Fix\n"
            "Use `datetime.now(timezone.utc)` for the cutoff calculation. Keep the existing "
            "`timedelta` logic intact.\n\n"
            "### Acceptance criteria\n"
            "- No `datetime.utcnow()` remains in `superset/commands/report/log_prune.py`.\n"
            "- `pre-commit run --all-files` passes.\n"
            "- Report pruning unit tests still pass."
        ),
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-label", action="store_true",
                        help="Also apply the trigger label to each created issue "
                             "(this fires the automation).")
    args = parser.parse_args()

    settings = get_settings()
    gh = GitHubClient(settings.github_token, settings.repo)

    if args.dry_run:
        print(f"Would ensure label '{TRIGGER_LABEL}' on {settings.repo}")
        for i in ISSUES:
            print(f"Would create issue: {i['title']}")
        return 0

    gh.ensure_label(TRIGGER_LABEL, LABEL_COLOR, LABEL_DESC)
    print(f"Ensured label '{TRIGGER_LABEL}'")
    if args.label_only:
        return 0

    created = []
    for spec in ISSUES:
        labels = [TRIGGER_LABEL] if args.apply_label else []
        issue = gh.create_issue(str(spec["title"]), str(spec["body"]), labels=labels)
        created.append({"number": issue["number"], "title": issue["title"],
                        "url": issue["html_url"]})
        print(f"Created #{issue['number']}: {issue['title']}")

    print(json.dumps(created, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
