"""Observability: metrics aggregation and Markdown summary generation."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    STATUS_BLOCKED,
    STATUS_DISPATCHED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    Run,
)

ALL_STATUSES = [
    STATUS_DISPATCHED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_BLOCKED,
]


def compute_metrics(db: Session) -> dict[str, Any]:
    """Aggregate run-level metrics that answer 'is this working?'."""
    runs = list(db.execute(select(Run)).scalars().all())
    total = len(runs)
    by_status = {s: 0 for s in ALL_STATUSES}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    terminal = sum(by_status[s] for s in TERMINAL_STATUSES)
    succeeded = by_status[STATUS_SUCCEEDED]
    pr_count = sum(1 for r in runs if r.pr_url)
    durations = [r.duration_seconds for r in runs if r.duration_seconds is not None]

    success_rate = (succeeded / terminal) if terminal else 0.0
    pr_rate = (pr_count / total) if total else 0.0

    total_acus = sum(r.acus_consumed or 0.0 for r in runs)
    succeeded_acus = sum(r.acus_consumed or 0.0 for r in runs if r.status == STATUS_SUCCEEDED)
    cost_per_fix = (succeeded_acus / succeeded) if succeeded else None

    # Live breakdown of in-flight runs by Devin status_detail (working /
    # waiting_for_user / waiting_for_approval / ...) so a stuck run is obvious.
    in_flight_detail: dict[str, int] = {}
    needs_attention = 0
    for r in runs:
        if r.status != STATUS_RUNNING:
            continue
        detail = r.status_detail or "unknown"
        in_flight_detail[detail] = in_flight_detail.get(detail, 0) + 1
        if detail in ("waiting_for_user", "waiting_for_approval"):
            needs_attention += 1

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_runs": total,
        "by_status": by_status,
        "in_flight": by_status[STATUS_DISPATCHED] + by_status[STATUS_RUNNING],
        "terminal": terminal,
        "succeeded": succeeded,
        "failed": by_status[STATUS_FAILED],
        "blocked": by_status[STATUS_BLOCKED],
        "pr_count": pr_count,
        "success_rate": round(success_rate, 3),
        "pr_rate": round(pr_rate, 3),
        "avg_duration_seconds": round(statistics.mean(durations), 1) if durations else None,
        "median_duration_seconds": round(statistics.median(durations), 1) if durations else None,
        "total_acus": round(total_acus, 2),
        "cost_per_fix_acus": round(cost_per_fix, 2) if cost_per_fix is not None else None,
        "in_flight_detail": in_flight_detail,
        "needs_attention": needs_attention,
    }


def render_summary_md(db: Session, path: str, *, repo: str = "") -> str:
    """Write a Markdown report (funnel + per-run table) and return its text."""
    m = compute_metrics(db)
    runs = list(db.execute(select(Run).order_by(Run.issue_number)).scalars().all())

    def pct(x: float) -> str:
        return f"{x * 100:.0f}%"

    lines: list[str] = []
    lines.append("# Remediation Report")
    lines.append("")
    if repo:
        lines.append(f"Repository: `{repo}`  ")
    lines.append(f"Generated: {m['generated_at']}")
    lines.append("")
    lines.append("## Funnel")
    lines.append("")
    lines.append("| Stage | Count |")
    lines.append("| --- | --- |")
    lines.append(f"| Issues dispatched | {m['total_runs']} |")
    lines.append(f"| In flight | {m['in_flight']} |")
    lines.append(f"| Completed | {m['terminal']} |")
    lines.append(f"| PRs opened | {m['pr_count']} |")
    lines.append(f"| Succeeded (verified) | {m['succeeded']} |")
    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    lines.append(f"- **Success rate (of completed):** {pct(m['success_rate'])} "
                 f"({m['succeeded']}/{m['terminal']})")
    lines.append(f"- **PR conversion:** {pct(m['pr_rate'])} ({m['pr_count']}/{m['total_runs']})")
    if m["median_duration_seconds"] is not None:
        lines.append(f"- **Median time-to-completion:** {m['median_duration_seconds']}s")
    if m["avg_duration_seconds"] is not None:
        lines.append(f"- **Average time-to-completion:** {m['avg_duration_seconds']}s")
    lines.append(f"- **Total ACUs consumed:** {m['total_acus']}")
    if m["cost_per_fix_acus"] is not None:
        lines.append(f"- **Cost per verified fix:** {m['cost_per_fix_acus']} ACUs")
    lines.append("")
    lines.append("## Runs")
    lines.append("")
    lines.append("| Issue | Status | Detail | Last update | Verdict | PR | ACUs | Duration | "
                 "Session |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in runs:
        dur = f"{r.duration_seconds:.0f}s" if r.duration_seconds is not None else "—"
        pr = f"[PR]({r.pr_url})" if r.pr_url else "—"
        sess = f"[link]({r.session_url})" if r.session_url else "—"
        acus = r.acus_consumed if r.acus_consumed is not None else "—"
        age = r.seconds_since_update
        last = f"{age:.0f}s ago" if age is not None else "—"
        lines.append(
            f"| #{r.issue_number} | {r.status} | {r.status_detail or '—'} | {last} | "
            f"{r.verdict or '—'} | {pr} | {acus} | {dur} | {sess} |"
        )
    lines.append("")

    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
