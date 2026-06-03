# Running & Infrastructure

How to run the orchestrator — either as a **credential-free simulation** or as a
**live** service in Docker — plus the infrastructure it needs.

## Contents
- [1. Prerequisites](#1-prerequisites)
- [2. Simulate with no credentials](#2-simulate-with-no-credentials)
- [3. Configuration](#3-configuration)
- [4. Run in Docker](#4-run-in-docker)
- [5. Run locally (no Docker)](#5-run-locally-no-docker)
- [6. Seed the issues](#6-seed-the-issues)
- [7. Trigger a remediation](#7-trigger-a-remediation)
- [8. Observe](#8-observe)
- [9. Infrastructure topology](#9-infrastructure-topology)
- [10. Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

- **Simulation only:** Python 3.11+ (or just Docker).
- **Live run:** a Devin **service-user API key** (`cog_…`) + **org ID**, and a
  **GitHub token** with `repo` scope (and `admin:repo_hook` if you want GitHub
  itself to deliver the webhook). The fork's **Issues** tab must be enabled.

## 2. Simulate with no credentials

The fastest way to see the whole pipeline (dispatcher → poller → reporting) work,
using fake Devin/GitHub clients that drive real code paths:

```bash
pip install -e ".[dev]"
python -m scripts.demo      # runs the pipeline end-to-end with fakes
cat summary.md              # the generated funnel + per-run report
pytest                      # full unit-test suite, no network
```

The demo terminates its fake sessions with scripted verdicts, so it populates the
exact columns a live run fills on termination — verdict, PR, ACUs, duration, and
**cost per verified fix** — proving the wiring independent of any credentials.

You can also run the simulation inside Docker without any secrets:

```bash
docker compose run --rm --no-deps --entrypoint python orchestrator -m scripts.demo
```

## 3. Configuration

All configuration is environment variables (see `.env.example`). Copy it and fill
in:

```bash
cp .env.example .env
```

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

> Secrets come only from the environment and are never logged or committed.

## 4. Run in Docker

The container (`Dockerfile`) runs `uvicorn` on port 8000 and persists SQLite +
`summary.md` to a mounted volume.

```bash
docker compose up --build
# then:
open http://localhost:8000/dashboard
curl http://localhost:8000/healthz
```

`docker-compose.yml` highlights:
- Maps host **8000 → 8000**.
- Reads Devin/GitHub config from your shell or `.env` (it **requires**
  `DEVIN_API_KEY` and `DEVIN_ORG_ID` to be set, and defaults the rest).
- Mounts a named volume `remediation-data` at `/data` so the run history and
  report **survive restarts** (`DB_PATH` / `SUMMARY_PATH` point there).
- `restart: unless-stopped` so the service comes back after a crash/reboot.

`/healthz` returns whether Devin and GitHub are configured — a quick readiness
check:

```json
{"ok": true, "repo": "MattBr0wne1/superset", "trigger_label": "devin-fix",
 "devin_configured": true, "github_configured": true}
```

## 5. Run locally (no Docker)

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload      # serves on :8000, starts the poller
```

## 6. Seed the issues

Creates the `devin-fix` label and the Part-1 issues on the fork (needs the fork's
Issues tab enabled and a `repo`-scoped token):

```bash
python -m scripts.seed_issues --dry-run    # preview
python -m scripts.seed_issues              # create label + issues
```

## 7. Trigger a remediation

There is **one logical trigger** (a labeled issue), reachable three ways:

- **Via GitHub (the real trigger):** add the `devin-fix` label to an issue. GitHub
  delivers `issues.labeled` to the webhook. This requires the orchestrator's port
  to be publicly reachable and a webhook registered on the fork (Settings →
  Webhooks → `POST <public-url>/webhooks/github-issue`, content type
  `application/json`, secret = `WEBHOOK_SECRET`, events = *Issues*).
- **Via CLI (manual replay):** `python -m app.cli dispatch --issue <N>` — the same
  `handle_labeled_issue` path, no public ingress needed.
- **Via webhook (manual):**
  ```bash
  curl -X POST localhost:8000/webhooks/github-issue \
    -H 'X-GitHub-Event: issues' -H 'Content-Type: application/json' \
    -d '{"action":"labeled","label":{"name":"devin-fix"},
         "issue":{"number":1,"title":"...","body":"..."}}'
  ```
  (Add a valid `X-Hub-Signature-256` header if `WEBHOOK_SECRET` is set.)

Re-triggering the same issue is a no-op by default (idempotent). To force a fresh
session, use the CLI force path (`dispatch --issue <N> --force`).

## 8. Observe

| Surface | What it shows |
| --- | --- |
| `GET /dashboard` | Auto-refreshing HTML: per-run status, live `status_detail`, "last update Ns ago" heartbeat, PR link, ACUs, duration, session link, plus headline cards. |
| `GET /api/metrics` | JSON: funnel counts, remediation rate, verified-pass rate, durations, total ACUs, cost-per-fix, in-flight breakdown, stalled / needs-attention. |
| `GET /api/runs` | JSON: every run row. |
| `summary.md` | Markdown funnel + per-run table, regenerated each poll. |
| Issue comments | Start, "PR raised — awaiting review", and terminal verdict. |

CLI equivalents (when not running the service): `python -m app.cli poll --loop`
and `python -m app.cli report`.

## 9. Infrastructure topology

```
            ┌──────────────┐  issues.labeled (HMAC)   ┌───────────────────────────┐
   GitHub   │  the fork    │ ───────────────────────► │  Orchestrator container    │
 (api +     │  repo        │ ◄─── comments / PR ────── │  uvicorn :8000             │
  webhook)  └──────────────┘     lookup (HTTPS)        │   • FastAPI (webhook+UI)   │
                  ▲                                     │   • asyncio poller (20s)   │
                  │ draft PR (by the Devin session)     │   • SQLite @ /data (vol)   │
                  │                                     └───────────┬───────────────┘
            ┌──────────────┐   create/get session (HTTPS)          │
            │  Devin v3    │ ◄─────────────────────────────────────┘
            │  api.devin.ai│ ──► each session runs on its own VM, opens the PR
            └──────────────┘
```

- **Stateless except for one SQLite file.** Back up / inspect `/data` to retain
  history; deleting it resets the run store.
- **Single process, single port.** No broker/queue — the poller is an in-process
  asyncio loop tied to the app lifespan.
- **Scaling note:** because dispatch is idempotent on `issue_number` and state is
  a local SQLite file, the design assumes a single instance. Running multiple
  replicas would require moving the store to a shared DB.

## 10. Troubleshooting

- **Webhook returns 401:** `WEBHOOK_SECRET` mismatch (signature verification
  failed). Ensure the GitHub webhook secret matches the env var.
- **GitHub can't reach the service / "site can't be reached":** the port isn't
  publicly reachable, or the tunnel uses basic-auth that the browser strips. The
  webhook only needs to be reachable by GitHub; use the CLI/manual path to drive
  runs without public ingress.
- **The Devin GitHub App can't register a webhook (403):** it lacks the
  "Repository webhooks" admin permission. Add the webhook manually in the fork
  settings, or use a PAT with `admin:repo_hook`, or drive via the CLI.
- **Issue comments stop appearing:** a GitHub **App installation token expires
  hourly**. Use a long-lived PAT for unattended runs. PR discovery via the Devin
  API is unaffected.
- **Verdict / Duration / Cost cells are blank:** these finalize only when a
  session **terminates** with a verdict. Sessions that opened a PR and sit in
  `pr_open` (awaiting review) haven't terminated, so those cells stay `—` by
  design. See [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).
- **A run looks stuck (`running`, stale heartbeat):** the poller auto-nudges once
  after `STALL_SECONDS`; if it stays stale it is flagged under "stalled / needs
  attention". Open the linked session to inspect.
