"""FastAPI application: webhook trigger, dashboard, and metrics API."""

from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from . import dispatcher
from .config import Settings, get_settings
from .devin_client import DevinClient
from .github_client import GitHubClient
from .models import Run, make_session_factory
from .poller import poller_loop, scan_loop
from .reporting import compute_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("remediation.main")


def build_clients(settings: Settings) -> tuple[DevinClient, GitHubClient | None]:
    devin = DevinClient(settings.devin_api_key, settings.devin_org_id, settings.devin_base_url)
    github = GitHubClient(settings.github_token, settings.repo) if settings.github_token else None
    return devin, github


def create_app(
    settings: Settings | None = None,
    *,
    devin: DevinClient | None = None,
    github: GitHubClient | None = None,
    session_factory: Any = None,
    start_poller: bool = True,
) -> FastAPI:
    """Build the FastAPI app. Dependencies can be injected for testing."""
    settings = settings or get_settings()
    session_factory = session_factory or make_session_factory(settings.database_url)
    if devin is None:
        devin, default_github = build_clients(settings)
        github = github or default_github

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        if not start_poller:
            yield
            return
        stop_event = asyncio.Event()
        tasks = [
            asyncio.create_task(
                poller_loop(session_factory=session_factory, devin=devin, github=github,
                            settings=settings, stop_event=stop_event)
            )
        ]
        if settings.label_scan_enabled and github is not None:
            tasks.append(
                asyncio.create_task(
                    scan_loop(session_factory=session_factory, devin=devin, github=github,
                              settings=settings, stop_event=stop_event)
                )
            )
        elif settings.label_scan_enabled:
            logger.warning("Label scan enabled but no GitHub token configured; scanner disabled")
        app.state.stop_event = stop_event
        app.state.poller_task = tasks[0]
        try:
            yield
        finally:
            stop_event.set()
            for task in tasks:
                await task

    app = FastAPI(title="Devin Superset Remediation", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.devin = devin
    app.state.github = github

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "repo": settings.repo,
            "trigger_label": settings.trigger_label,
            "devin_configured": bool(settings.devin_api_key and settings.devin_org_id),
            "github_configured": github is not None,
            "label_scan_active": settings.label_scan_enabled and github is not None,
        }

    @app.post("/webhooks/github-issue")
    async def github_issue_webhook(
        request: Request,
        x_github_event: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> JSONResponse:
        from .security import verify_github_signature

        body = await request.body()
        if not verify_github_signature(body, x_hub_signature_256, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        if x_github_event == "ping":
            return JSONResponse({"ok": True, "pong": True})
        if x_github_event != "issues":
            return JSONResponse({"ok": True, "ignored": f"event={x_github_event}"})

        payload = await request.json()
        action = payload.get("action")
        label_name = (payload.get("label") or {}).get("name")
        if action != "labeled" or label_name != settings.trigger_label:
            return JSONResponse(
                {"ok": True, "ignored": f"action={action} label={label_name}"}
            )

        issue = payload["issue"]

        def _dispatch() -> dict[str, Any]:
            with session_factory() as db:
                run = dispatcher.handle_labeled_issue(
                    issue, db=db, devin=devin, github=github, settings=settings
                )
                return run.to_dict()

        result = await asyncio.to_thread(_dispatch)
        return JSONResponse({"ok": True, "run": result}, status_code=202)

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        with session_factory() as db:
            runs = db.execute(select(Run).order_by(Run.issue_number)).scalars().all()
            return {"items": [r.to_dict() for r in runs]}

    @app.get("/api/metrics")
    def metrics() -> dict[str, Any]:
        with session_factory() as db:
            return compute_metrics(db, stall_seconds=settings.stall_seconds)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        with session_factory() as db:
            m = compute_metrics(db, stall_seconds=settings.stall_seconds)
            runs = [r.to_dict() for r in
                    db.execute(select(Run).order_by(Run.issue_number)).scalars().all()]
        return _render_dashboard(m, runs, settings)

    return app


def _render_dashboard(metrics: dict[str, Any], runs: list[dict[str, Any]],
                      settings: Settings) -> str:
    """Render a dependency-light HTML dashboard."""
    rows = []
    for r in runs:
        pr_url = html.escape(r["pr_url"], quote=True) if r["pr_url"] else ""
        sess_url = html.escape(r["session_url"], quote=True) if r["session_url"] else ""
        pr = f'<a href="{pr_url}">PR</a>' if r["pr_url"] else "—"
        sess = f'<a href="{sess_url}">session</a>' if r["session_url"] else "—"
        dur = f'{r["duration_seconds"]:.0f}s' if r["duration_seconds"] is not None else "—"
        acus = r["acus_consumed"] if r["acus_consumed"] is not None else "—"
        badge = {
            "succeeded": "#2da44e", "failed": "#cf222e", "blocked": "#bf8700",
            "running": "#0969da", "dispatched": "#57606a", "pr_open": "#8250df",
        }.get(r["status"], "#57606a")
        status_label = "PR open" if r["status"] == "pr_open" else r["status"]
        if r["status"] == "pr_open":
            # Once a PR is raised, the meaningful state is "awaiting human review",
            # not the raw session detail (e.g. waiting_for_user).
            detail_cell = '<span style="color:#8250df;font-weight:600">awaiting review/merge</span>'
        else:
            detail = r.get("status_detail") or "—"
            age0 = r.get("seconds_since_update")
            stalled = (r["status"] == "running" and not r["pr_url"]
                       and age0 is not None and age0 > settings.stall_seconds)
            attn = detail in ("waiting_for_user", "waiting_for_approval")
            detail_safe = html.escape(detail)
            if stalled:
                detail_cell = '<span style="color:#cf222e;font-weight:600">stalled</span>'
            elif attn:
                detail_cell = f'<span style="color:#bf8700;font-weight:600">{detail_safe}</span>'
            else:
                detail_cell = detail_safe
        age = r.get("seconds_since_update")
        last = f"{age:.0f}s ago" if age is not None else "—"
        title_safe = html.escape(r["issue_title"] or "")
        verdict_safe = html.escape(r["verdict"]) if r["verdict"] else "—"
        rows.append(
            f"<tr><td>#{r['issue_number']}</td>"
            f"<td>{title_safe}</td>"
            f'<td><span style="background:{badge};color:#fff;padding:2px 8px;'
            f'border-radius:10px;font-size:12px">{status_label}</span></td>'
            f"<td>{detail_cell}</td><td>{last}</td>"
            f"<td>{verdict_safe}</td><td>{pr}</td><td>{acus}</td>"
            f"<td>{dur}</td><td>{sess}</td></tr>"
        )
    table = "\n".join(rows) or '<tr><td colspan="10">No runs yet</td></tr>'
    rr = metrics["pr_rate"] * 100
    cpf = metrics["cost_per_fix_acus"]
    cards_data = [
        (metrics["total_runs"], "Total runs"),
        (metrics["in_flight"], "In flight"),
        (metrics.get("awaiting_review", 0), "PR awaiting review"),
        (metrics.get("stalled", 0), "Stalled"),
        (metrics.get("needs_attention", 0), "Needs attention"),
        (metrics["pr_count"], "PRs opened"),
        (f"{rr:.0f}%", "Remediation rate"),
        (metrics["succeeded"], "Verified pass"),
        (f"{cpf} ACU" if cpf is not None else "—", "Cost / fix"),
    ]
    detail_breakdown = metrics.get("in_flight_detail") or {}
    breakdown_txt = (
        " · ".join(f"{v} {k}" for k, v in sorted(detail_breakdown.items()))
        if detail_breakdown else "none in flight"
    )
    cards = "\n".join(
        f'  <div class="card"><div class="n">{value}</div>'
        f'<div class="l">{label}</div></div>'
        for value, label in cards_data
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Remediation Dashboard</title>
<meta http-equiv="refresh" content="15">
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1f2328}}
.cards{{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}}
.card{{border:1px solid #d0d7de;border-radius:8px;padding:16px;min-width:140px}}
.card .n{{font-size:28px;font-weight:700}}
.card .l{{color:#57606a;font-size:13px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #d0d7de;padding:8px;text-align:left;font-size:14px}}
th{{background:#f6f8fa}}
</style></head>
<body>
<h1>Devin Remediation Dashboard</h1>
<p>Repository: <code>{settings.repo}</code> · trigger label: <code>{settings.trigger_label}</code>
 · auto-refresh 15s</p>
<p style="color:#57606a;font-size:13px">In-flight detail: {breakdown_txt}</p>
<div class="cards">
{cards}
</div>
<table>
<thead><tr><th>Issue</th><th>Title</th><th>Status</th><th>Detail</th><th>Last update</th>
<th>Verdict</th><th>PR</th><th>ACUs</th><th>Duration</th><th>Session</th></tr></thead>
<tbody>
{table}
</tbody></table>
</body></html>"""


app = create_app()
