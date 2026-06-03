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
        │  push ──────►│  │  webhook   │──►│  dispatcher  │            │
        │              │  │  handler   │   │ (idempotent) │            │
        │  pull ──────►│  ├────────────┤──►│              │            │
        └─ label scan  │  │  scanner   │   └──────┬───────┘            │
                       │  └────────────┘          │ create_session     │
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

**One label, two ways to deliver it.** Both entry points converge on the same
idempotent `dispatcher.handle_labeled_issue`:
- **Pull (default, local-friendly):** a background **scanner** polls the repo for
  open `devin-fix`-labeled issues and dispatches new ones — no public URL needed.
- **Push (production):** GitHub delivers the `issues.labeled` event to the
  **FastAPI webhook** `POST /webhooks/github-issue` (HMAC-verified) for instant
  dispatch.

The dispatcher is deliberately trigger-agnostic, so any other event source (a Snyk
or Dependabot scan, a cron job, a CLI replay) "slots into the same handler" by
calling it with an issue payload — no changes to the core. `app/cli.py` exposes
`scan` / `dispatch` / `poll` / `report` for exactly this kind of manual/automated replay.

## Components

| File | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app: webhook, label-scanner + poller lifecycle, `/dashboard`, `/api/runs`, `/api/metrics` |
| `app/dispatcher.py` | Idempotent: labeled issue → Devin session → persisted `Run`; `scan_and_dispatch` for the pull trigger |
| `app/devin_client.py` | Devin v3 client (`create_session`, `get_session`) |
| `app/github_client.py` | GitHub REST (issues, `list_issues_by_label`, comments, labels) |
| `app/prompts.py` | Builds the session prompt incl. the `structured_output` verdict contract |
| `app/poller.py` | Background loops: reconcile sessions → status/verdict/PR; periodic label scan |
| `app/reporting.py` | Metrics aggregation + `summary.md` report |
| `app/models.py` | SQLite `Run` model (the observability store) |
| `app/cli.py` | `scan` / `dispatch` / `poll` / `report` ops commands (manual replay) |
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

> Everything runs in Docker — there is no separate local/host setup to maintain.

### 0. Simulate with no credentials (recommended first look)

The fastest way to see the whole pipeline (dispatcher → poller → reporting) work,
using fake Devin/GitHub clients that drive the real code paths. The demo
terminates its fake sessions with scripted verdicts, so it populates the exact
columns a live run fills on termination — verdict, PR, ACUs, duration, and
cost-per-verified-fix.

Build the image, then run the demo. This bypasses `docker compose` (which
requires Devin credentials for the live service) so it runs with no secrets:
```bash
docker build -t devin-superset-remediation .
docker run --rm --entrypoint python devin-superset-remediation -m scripts.demo
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

`docker compose` automatically reads the `.env` file from the project directory
and injects those variables into the container. The container runs `uvicorn` on
port 8000 and persists the SQLite store + `summary.md` to the named volume
`remediation-data` (mounted at `/data`), so run history survives restarts.
`docker-compose.yml` requires `DEVIN_API_KEY` and `DEVIN_ORG_ID` and defaults the
rest. `/healthz` reports readiness:

```json
{"ok": true, "repo": "MattBr0wne1/superset", "trigger_label": "devin-fix",
 "devin_configured": true, "github_configured": true, "label_scan_active": true}
```

### 3. Author the Part-1 issues on the fork
Only needed if the issues don't exist yet (the demo issues are already seeded on
the fork). Requires the fork's Issues tab enabled and a valid `GITHUB_TOKEN`:
```bash
docker compose exec orchestrator python -m scripts.seed_issues --dry-run   # preview
docker compose exec orchestrator python -m scripts.seed_issues             # create label + issues
```

### 4. Trigger remediation

**Recommended (local, fully event-driven): label the issue on GitHub.**
The running container scans the repo on an interval for open issues carrying the
`devin-fix` label and dispatches any it hasn't seen yet — no public URL, no CLI
call. Just add the label to an issue (e.g. #7/#8/#9) and watch the dashboard.
```text
add "devin-fix" label on GitHub  →  next scan tick picks it up  →  Devin session → draft PR
```
> This is the "pull" form of the event trigger. It requires a **valid**
> `GITHUB_TOKEN` in `.env` (it reads the repo's issues). Tunables:
> `LABEL_SCAN_ENABLED` (default `true`) and `LABEL_SCAN_INTERVAL_SECONDS`
> (default `60`). The scan is idempotent — an already-dispatched issue is never
> re-run. Confirm it's active via `label_scan_active: true` on `/healthz`.

Other trigger paths (same dispatch code, useful for specific situations):
- **One-shot scan on demand:** `docker compose exec orchestrator python -m app.cli scan`
- **Dispatch a single issue explicitly:** `docker compose exec orchestrator python -m app.cli dispatch --issue <N>`
- **Webhook (push):** the production "instant" trigger — GitHub delivers
  `issues.labeled` to `POST /webhooks/github-issue` (HMAC-verified). Needs the
  port publicly reachable + a webhook registered on the repo. Simulate locally
  with no token (the issue is in the payload):
  ```bash
  curl -X POST localhost:8000/webhooks/github-issue \
    -H 'X-GitHub-Event: issues' -H 'Content-Type: application/json' \
    -d '{"action":"labeled","label":{"name":"devin-fix"},
         "issue":{"number":1,"title":"...","body":"..."}}'
  ```

> Token note: a GitHub App `ghs_…` token expires hourly — use a Personal Access
> Token with `repo` scope so it doesn't lapse mid-run.

### 5. Watch it work
The service's background poller reconciles automatically; open
`http://localhost:8000/dashboard`. To run the ops commands manually inside the
container:
```bash
docker compose exec orchestrator python -m app.cli report   # regenerate summary.md + print metrics
docker compose logs -f orchestrator                         # follow the poller's state transitions
```

### End-to-end worked example (local Docker)
The full path for triggering the three seeded issues from your own machine:
```bash
git clone https://github.com/MattBr0wne1/devin-superset-remediation.git
cd devin-superset-remediation && git checkout feat/orchestrator

cp .env.example .env        # then edit .env, e.g.:
#   DEVIN_API_KEY=cog_...           (Devin → Settings → API keys)
#   DEVIN_ORG_ID=org-...            (your Devin org id)
#   GITHUB_TOKEN=ghp_...            (PAT, repo scope — read issues + comment)
#   REPO=MattBr0wne1/superset
#   TRIGGER_LABEL=devin-fix

docker compose up -d --build       # start service + poller + label-scanner + dashboard

# Then just label the issues on GitHub (#7/#8/#9 with "devin-fix"). The scanner
# picks them up within LABEL_SCAN_INTERVAL_SECONDS and dispatches each one.
# Prefer not to wait for the interval? force an immediate scan:
docker compose exec orchestrator python -m app.cli scan
# watch http://localhost:8000/dashboard — each labeled issue → a real Devin session → draft PR
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
| `LABEL_SCAN_ENABLED` | no | `true` | Background scan of the repo for labeled issues (the pull trigger). Needs a valid `GITHUB_TOKEN`. |
| `LABEL_SCAN_INTERVAL_SECONDS` | no | `60` | How often the scanner polls the repo for newly-labeled issues. |
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
