# Devin Superset Remediation

An **event-driven automation** that remediates GitHub issues on a fork of
[Apache Superset](https://github.com/apache/superset) using the **Devin API**.

Labeling an issue `devin-fix` triggers a service that programmatically creates and
manages a **Devin session**. The session fixes the issue on a branch, **verifies
its own work** (`pre-commit` + tests), opens a **draft PR**, and returns a
machine-readable verdict. A background poller records every run in SQLite and
exposes a live dashboard, a metrics API, issue comments, and a Markdown report.

> **Why this is more than Dependabot:** Dependabot bumps versions and opens a PR
> regardless of whether anything works. Here, a Devin session implements the fix
> *and proves it green* before the PR is opened — the verdict (`pass`/`fail`) is
> recorded as an objective signal.

> **New here?** Jump to [Quick start §0](#0-simulate-with-no-credentials) — it
> runs the whole pipeline with no secrets. Everything you need to run or simulate
> the workflow is in this README; the docs below are optional deep-dives.

## Documentation

Run/simulate instructions live in this README (see [Quick start](#quick-start)).
For background:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, data flow, lifecycle
  states, the SQLite store, stall self-healing, infrastructure footprint.
- [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) — why each choice was made
  (draft PRs, single trigger, verify-before-PR, observability, what was cut).

---

## Architecture

```
                       ┌──────────────────────────────────────────────┐
   label "devin-fix"   │                Orchestrator                   │
  ───────────────────► │                                              │
  (GitHub issue)       │  ┌────────────┐   ┌──────────────┐           │
        │              │  │  webhook   │──►│  dispatcher  │            │
        └─ webhook ───►│  │  handler   │   │ (idempotent) │            │
                       │  └────────────┘   └──────┬───────┘            │
                       │                          │ create_session     │
                       │                          ▼                    │
                       │                   ┌──────────────┐  Devin v3   │
                       │                   │ DevinClient  │────────────►│ api.devin.ai
                       │   ┌───────────┐   └──────┬───────┘            │
                       │   │  poller   │◄─────────┘ get_session        │
                       │   │ (asyncio) │   reconcile → SQLite          │
                       │   └─────┬─────┘                               │
                       │         │ verdict / PR / status               │
                       │         ▼                                     │
                       │  observability: /dashboard, /api/metrics,     │
                       │  summary.md, issue comments                   │
                       └──────────────────────────────────────────────┘
                                                  │ opens draft PR + comments
                                                  ▼
                                      MattBr0wne1/superset (the fork)
```

**One trigger:** a `devin-fix`-labeled issue is delivered to the **FastAPI webhook**
`POST /webhooks/github-issue` (HMAC-verified), which calls
`dispatcher.handle_labeled_issue`.

The dispatcher is deliberately trigger-agnostic, so any other event source (a Snyk
or Dependabot scan, a cron job, a CLI replay) "slots into the same handler" by
calling it with an issue payload — no changes to the core. `app/cli.py` exposes
`dispatch` / `poll` / `report` for exactly this kind of manual/automated replay.

## Components

| File | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app: webhook, `/dashboard`, `/api/runs`, `/api/metrics`, poller lifecycle |
| `app/dispatcher.py` | Idempotent: labeled issue → Devin session → persisted `Run` |
| `app/devin_client.py` | Devin v3 client (`create_session`, `get_session`) |
| `app/github_client.py` | GitHub REST (issues, comments, labels, hooks, repo admin) |
| `app/prompts.py` | Builds the session prompt incl. the `structured_output` verdict contract |
| `app/poller.py` | Background loop: reconcile sessions → status/verdict/PR, comment back |
| `app/reporting.py` | Metrics aggregation + `summary.md` report |
| `app/models.py` | SQLite `Run` model (the observability store) |
| `app/cli.py` | `dispatch` / `poll` / `report` ops commands (manual replay) |
| `scripts/seed_issues.py` | Creates the `devin-fix` label and the Part-1 issues |
| `scripts/demo.py` | **Credential-free simulation** of the whole pipeline |

## Observability — "how an engineering leader knows it's working"

- **`GET /dashboard`** — auto-refreshing HTML: per-run status, live
  `status_detail`, a "last update Ns ago" heartbeat, verdict, PR link, ACUs,
  duration, session link, plus headline cards (total, in-flight, PRs awaiting
  review, stalled, **remediation rate**, verified pass, cost-per-fix).
- **`GET /api/metrics`** — JSON: funnel counts, success rate, PR conversion,
  median/avg time-to-completion, total ACUs, and **cost-per-verified-fix**
  (real ACU usage read from each session's `acus_consumed`).
- **`summary.md`** — regenerated each poll (funnel + per-run table).
- **Issue comments** — start + terminal verdict, so the audit trail lives on the
  work item itself.
- **Structured logs** for every state transition.

## Quick start

### 0. Simulate with no credentials (recommended first look)

The fastest way to see the whole pipeline (dispatcher → poller → reporting) work,
using fake Devin/GitHub clients that drive the real code paths. The demo
terminates its fake sessions with scripted verdicts, so it populates the exact
columns a live run fills on termination — verdict, PR, ACUs, duration, and
cost-per-verified-fix.

```bash
pip install -e ".[dev]"
python -m scripts.demo        # runs dispatcher+poller+reporting with fakes
cat summary.md
pytest                        # full unit-test suite (no network)
```

Or inside Docker, still with no secrets:
```bash
docker compose run --rm --no-deps --entrypoint python orchestrator -m scripts.demo
```

### 1. Configure
```bash
cp .env.example .env          # fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, ...
```

### 2. Run the service (Docker)
```bash
docker compose up --build
# open http://localhost:8000/dashboard  and  http://localhost:8000/healthz
```

The container runs `uvicorn` on port 8000 and persists the SQLite store +
`summary.md` to the named volume `remediation-data` (mounted at `/data`), so run
history survives restarts. `docker-compose.yml` requires `DEVIN_API_KEY` and
`DEVIN_ORG_ID` and defaults the rest. `/healthz` reports readiness:

```json
{"ok": true, "repo": "MattBr0wne1/superset", "trigger_label": "devin-fix",
 "devin_configured": true, "github_configured": true}
```

Or locally (no Docker):
```bash
uvicorn app.main:app --reload
```

### 3. Author the Part-1 issues on the fork
```bash
# Requires the fork's Issues tab to be enabled and a token with repo scope.
python -m scripts.seed_issues --dry-run     # preview
python -m scripts.seed_issues               # create label + issues
```

### 4. Trigger remediation
- **Via GitHub (the trigger):** add the `devin-fix` label to an issue — GitHub
  delivers the `issues.labeled` event to the webhook.
- **Via CLI (manual replay):** `python -m app.cli dispatch --issue <N>`
- **Via webhook (manual):**
  ```bash
  curl -X POST localhost:8000/webhooks/github-issue \
    -H 'X-GitHub-Event: issues' -H 'Content-Type: application/json' \
    -d '{"action":"labeled","label":{"name":"devin-fix"},
         "issue":{"number":1,"title":"...","body":"..."}}'
  ```

### 5. Watch it work
```bash
python -m app.cli poll --loop        # or rely on the service's background poller
python -m app.cli report             # regenerate summary.md + print metrics
```

## The Part-1 issues

Small, self-contained, and verifiable via `pre-commit` (so a session can prove
the fix green). See `scripts/seed_issues.py` for exact text. Theme: removing
deprecated `datetime.utcnow()` (Python 3.12) — e.g. in `superset/utils/dates.py`
(a real correctness bug: `utcnow()` is naive, so the subsequent `astimezone`
mis-converts on non-UTC hosts).

## Configuration reference

All configuration is environment variables (see `.env.example`):

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DEVIN_API_KEY` | live | — | Devin service-user key (`cog_…`). |
| `DEVIN_ORG_ID` | live | — | Devin organization ID (sessions are org-scoped). |
| `DEVIN_BASE_URL` | no | `https://api.devin.ai/v3` | Devin API base. |
| `CREATE_AS_USER_ID` | no | — | Attribute sessions to a human (needs `ImpersonateOrgSessions`). |
| `GITHUB_TOKEN` | live* | — | Token for issue comments + PR lookup. Without it, runs still work but no comments are posted. |
| `REPO` | no | `MattBr0wne1/superset` | The fork that owns issues + receives PRs. |
| `TRIGGER_LABEL` | no | `devin-fix` | Label that fires a run. |
| `WEBHOOK_SECRET` | recommended | — | Shared secret for `X-Hub-Signature-256` verification. |
| `DB_PATH` | no | `remediation.db` (`/data/...` in Docker) | SQLite path. |
| `SUMMARY_PATH` | no | `summary.md` (`/data/...` in Docker) | Markdown report path. |
| `POLL_INTERVAL_SECONDS` | no | `20` | How often the poller reconciles. |
| `STALL_SECONDS` | no | `360` | Heartbeat age after which a PR-less running session is nudged/flagged. |

Secrets come only from the environment and are never logged or committed.

## Infrastructure

- **One Python 3.11 container** (`Dockerfile`), `uvicorn` on port 8000; FastAPI
  serves the webhook + observability endpoints and an in-process asyncio poller
  reconciles sessions every `POLL_INTERVAL_SECONDS`.
- **State is one SQLite file** on a mounted volume — no external DB, broker, or
  cache. Back up / inspect `/data` to retain history.
- **Outbound:** HTTPS to `api.devin.ai` (sessions) and `api.github.com`
  (comments / PR lookup). **Inbound:** port 8000 for the webhook + dashboard.
- **Single instance by design:** dispatch idempotency and state are local, so
  horizontal scaling would require moving the store to a shared DB.

## Troubleshooting

- **Webhook returns 401:** `WEBHOOK_SECRET` mismatch (signature verification
  failed) — ensure the GitHub webhook secret matches the env var.
- **GitHub can't reach the service:** the port isn't publicly reachable. The
  webhook only needs to be reachable by GitHub; use the CLI/manual path to drive
  runs without public ingress.
- **The Devin GitHub App can't register a webhook (403):** it lacks the
  "Repository webhooks" admin permission — register the hook manually in the fork
  settings, use a PAT with `admin:repo_hook`, or drive via the CLI.
- **Issue comments stop appearing:** a GitHub **App installation token expires
  hourly** — use a long-lived PAT for unattended runs. PR discovery via the Devin
  API is unaffected.
- **Verdict / Duration / Cost cells are blank:** these finalize only when a
  session **terminates** with a verdict. A session that opened a PR and sits in
  `pr_open` (awaiting review) hasn't terminated, so those cells stay `—` by
  design — see [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).
- **A run looks stuck (`running`, stale heartbeat):** the poller auto-nudges once
  after `STALL_SECONDS`; if it stays stale it is flagged under "stalled / needs
  attention". Open the linked session to inspect.

## Security notes
- Webhook requests are verified with `X-Hub-Signature-256` (set `WEBHOOK_SECRET`).
- Dispatch is idempotent per issue number — redelivered webhooks/label toggles do
  not spawn duplicate sessions.
- No secrets are logged; all credentials come from the environment.

## Development
```bash
pip install -e ".[dev]"
ruff check .
mypy app
pytest
```
