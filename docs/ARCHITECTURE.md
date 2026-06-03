# Architecture

This document describes the system that turns a labeled GitHub issue into a
verified, Devin-authored draft pull request, and the observability surfaces that
let a human know it is working.

## 1. System overview

```
                          ┌──────────────────────────────────────────────────────┐
  label "devin-fix"       │                     Orchestrator (one process)         │
  on a GitHub issue       │                                                        │
        │                 │   ┌────────────┐      ┌──────────────────┐            │
        │  push: webhook  │   │  FastAPI   │ ───► │   dispatcher     │            │
        ├───────────────►│   │  webhook   │      │ (idempotent;     │            │
        │                 │   │  handler   │      │  trigger-agnostic)│           │
        │  pull: scan     │   ├────────────┤ ───► │                  │            │
        └───────────────►│   │  scanner   │      └────────┬─────────┘            │
                          │   └────────────┘               │ create_session       │
                          │                                ▼                       │
                          │                       ┌──────────────────┐  Devin v3  │
                          │                       │   DevinClient    │ ─────────► │ api.devin.ai
                          │   ┌───────────────┐   └────────┬─────────┘            │
                          │   │    poller     │ ◄──────────┘ get_session (20s)    │
                          │   │  (asyncio,    │     reconcile → SQLite            │
                          │   │  background)  │                                    │
                          │   └──────┬────────┘                                    │
                          │          │ status / status_detail / PR / verdict / ACUs│
                          │          ▼                                             │
                          │   SQLite (runs table)  ──►  /dashboard, /api/metrics,  │
                          │                              /api/runs, summary.md      │
                          └──────────────────────────────────────────────┬────────┘
                                                                          │ opens draft PR
                                                                          │ + issue comments
                                                                          ▼
                                                          MattBr0wne1/superset (the fork)
```

Everything runs in **one process** (one container): the FastAPI app serves the
webhook + observability endpoints, and two `asyncio` background tasks run on
intervals — the **poller** (reconciles live Devin sessions) and the **scanner**
(polls the repo for `devin-fix`-labeled issues and dispatches new ones, the
local-friendly pull trigger). The only persistent state is a single **SQLite**
file.

## 2. Components

| File | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app: webhook endpoint, `/healthz`, `/dashboard`, `/api/runs`, `/api/metrics`, and the poller + scanner lifecycle (started/stopped with the app). |
| `app/dispatcher.py` | The single, trigger-agnostic entry point. Idempotently turns a labeled issue into a Devin session and a persisted `Run`; `scan_and_dispatch` drives the pull trigger. |
| `app/devin_client.py` | Minimal Devin v3 client: `create_session`, `get_session`, `send_message` (nudge), `terminate_session`. |
| `app/github_client.py` | Minimal GitHub REST client: issue comments, labels, PR-by-branch lookup. |
| `app/prompts.py` | Builds the session prompt — the contract that tells Devin what to fix, how to verify, and to emit a machine-readable verdict. |
| `app/poller.py` | Background loops: reconcile each active session → status/verdict/PR (post comments, self-heal stalls); and periodically scan the repo for newly-labeled issues. |
| `app/reporting.py` | Metrics aggregation (`compute_metrics`) and the Markdown `summary.md`. |
| `app/models.py` | SQLite `Run` model and lifecycle status constants — the observability store. |
| `app/config.py` | Pydantic settings loaded from environment / `.env`. |
| `app/security.py` | HMAC verification of GitHub webhook signatures. |
| `app/cli.py` | `scan` / `dispatch` / `poll` / `report` ops commands (manual replay path). |
| `scripts/seed_issues.py` | Creates the `devin-fix` label and the seed issues on the fork. |
| `scripts/demo.py` | Credential-free simulation of the whole pipeline with fake clients. |

## 3. End-to-end sequence

1. A maintainer adds the `devin-fix` label to an issue on the fork.
2. GitHub delivers an `issues.labeled` event to `POST /webhooks/github-issue`.
3. `app/security.py` verifies the `X-Hub-Signature-256` HMAC. The handler ignores
   any event that is not `action=labeled` with the configured trigger label.
4. `dispatcher.handle_labeled_issue` runs (idempotent per issue number):
   - inserts a `Run` row (`status=dispatched`),
   - builds the prompt (`app/prompts.py`),
   - calls Devin v3 `create_session`,
   - stores `session_id` / `session_url`, flips `status=running`,
   - posts a "remediation started" comment on the issue.
5. The Devin session — on its **own VM** — checks out the fork, makes the focused
   fix on branch `devin/fix-issue-<n>`, runs `pre-commit` on the changed files +
   relevant tests, opens a **draft PR**, and sets its `structured_output` verdict.
6. The **poller** (every `POLL_INTERVAL_SECONDS`, default 20s) calls
   `get_session` for each active run and reconciles:
   - records `status`, `status_detail`, `acus_consumed`, `pr_url`, verdict,
   - if the session hasn't surfaced a PR yet, falls back to looking it up by the
     deterministic branch name on GitHub,
   - posts a "PR raised — awaiting review" comment the first time a PR appears,
   - posts a terminal verdict comment if/when the session terminates,
   - sends a one-shot **nudge** to a session that has gone silent (see §6).
7. The dashboard, metrics API, and `summary.md` all read from the same SQLite
   store, so they always reflect the latest reconciled state.

## 4. Lifecycle states

There are **two** distinct state machines, deliberately decoupled.

### Orchestrator lifecycle (`app/models.py`)
What the *automation* thinks is happening, used by all observability:

| Status | Meaning | Terminal? |
| --- | --- | --- |
| `dispatched` | `Run` created, session being created | no (active) |
| `running` | Session working, no PR yet | no (active) |
| `pr_open` | A draft PR exists; automation's job is done, human owns review | no (active) |
| `succeeded` | Session terminated with `verdict=pass` (or a PR and no explicit fail) | yes |
| `failed` | Session terminated with `verdict=fail` (or terminated with no PR) | yes |
| `blocked` | Session suspended/halted needing attention | yes |

`pr_open` is intentionally **non-terminal**: the PR is the deliverable, but the
session stays alive (it can answer review comments / fix CI), so the orchestrator
keeps reconciling it until it truly terminates.

### Devin session status (read from the v3 API)
The raw signal the poller maps from. `status` ∈ {`running`, `exit`, `error`,
`suspended`, …}; `status_detail` ∈ {`working`, `waiting_for_user`,
`waiting_for_approval`, `finished`, …}. A session is treated as **terminal** when
`status` ∈ {`exit`, `error`, …} or `status_detail == finished`, and as
**blocked** when `status` ∈ {`suspended`, `blocked`}. See
`SessionState.is_terminal` / `is_blocked` in `app/devin_client.py`.

The mapping lives in `_derive_status` (`app/poller.py`): a non-terminal session
becomes `pr_open` if a PR exists, else `running`; a terminal session becomes
`succeeded` / `failed` / `blocked` based on the verdict and PR presence.

## 5. Data model (the observability store)

A single SQLite table, `runs`, one row per issue (`issue_number` is unique — this
is what makes dispatch idempotent). Key columns (`app/models.py`):

- Identity: `issue_number`, `issue_title`, `session_id`, `session_url`
- Orchestrator state: `status`
- Live Devin signal: `status_enum`, `status_detail`, `session_updated_at`
  (heartbeat), `nudged_at`
- Result: `acus_consumed`, `verdict`, `pr_url`, `checks_json`, `summary`, `error`
- Timing: `created_at`, `updated_at`, `completed_at`

Two derived properties power the liveness signals:
- `duration_seconds` = `completed_at − created_at` (UTC-normalized, since SQLite
  drops tzinfo on read).
- `seconds_since_update` = age of the Devin heartbeat — how staleness/stall is
  detected.

## 6. Stall self-healing

Sessions work "heads-down": they emit very few events while busy, so the v3
`updated_at` heartbeat and `acus_consumed` can stay flat for minutes during real
work — making a healthy session look dead. To stay reliable without paving over
genuine hangs, the poller applies a **one-shot auto-nudge**:

- If a run is `running`, has **no PR**, has **not been nudged**, and its heartbeat
  is older than `STALL_SECONDS` (default 360s), the poller sends one message
  asking the session to continue and finalize (`_maybe_nudge_stalled`).
- `nudged_at` is stamped so it never nudges the same run twice.
- If still stale afterward, the run is surfaced as **stalled** / **needs
  attention** in the metrics so a human notices.

This provably recovered sessions that had silently hung at startup.

## 7. Extensibility

`dispatcher.handle_labeled_issue` is the single choke point and takes a plain
issue dict, so any new trigger "slots in" by calling it:

- the label scanner (the default pull trigger — no public URL needed),
- the FastAPI webhook (the push trigger),
- `app/cli.py scan` / `dispatch --issue <N>` (manual/automated replay),
- a hypothetical scanner (Snyk/Dependabot/cron) that produces an issue payload.

No core changes are needed to add a transport — only a new caller.

## 8. Infrastructure footprint

- **Runtime:** one Python 3.11 container (`Dockerfile`), `uvicorn` serving on
  port 8000.
- **State:** one SQLite file on a mounted volume (`/data` in Docker). No external
  database, broker, or cache.
- **Outbound:** HTTPS to `api.devin.ai` (sessions) and `api.github.com`
  (comments / PR lookup).
- **Inbound:** port 8000 for observability endpoints (and the optional webhook).
  The default **pull scanner needs no inbound exposure** — it reaches out to
  GitHub. Only the push webhook requires the port to be publicly reachable
  (tunnel/ingress) plus a hook registered on the fork.

See the [README](../README.md) for how to run/simulate it and
[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) for the rationale behind these choices.
