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

- **`GET /dashboard`** — auto-refreshing HTML: per-run status, verdict, PR link,
  ACUs, duration, session link, plus headline cards (total, in-flight, PRs,
  success rate, cost-per-fix).
- **`GET /api/metrics`** — JSON: funnel counts, success rate, PR conversion,
  median/avg time-to-completion, total ACUs, and **cost-per-verified-fix**
  (real ACU usage read from each session's `acus_consumed`).
- **`summary.md`** — regenerated each poll (funnel + per-run table).
- **Issue comments** — start + terminal verdict, so the audit trail lives on the
  work item itself.
- **Structured logs** for every state transition.

## Quick start

### 0. Simulate with no credentials (recommended first look)
```bash
pip install -e ".[dev]"
python -m scripts.demo        # runs dispatcher+poller+reporting with fakes
cat summary.md
pytest                        # full unit-test suite (no network)
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

Or locally:
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

See `.env.example`. Key variables: `DEVIN_API_KEY`, `DEVIN_ORG_ID`,
`DEVIN_BASE_URL`, `CREATE_AS_USER_ID`, `GITHUB_TOKEN`, `REPO`, `TRIGGER_LABEL`,
`WEBHOOK_SECRET`, `DB_PATH`, `SUMMARY_PATH`, `POLL_INTERVAL_SECONDS`.

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
