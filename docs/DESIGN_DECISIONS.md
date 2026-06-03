# Design Decisions

The goal is a **simple but reliable** event-driven workflow where observability
is key. Every decision below is weighed against that: does it make the core
workflow more reliable or more observable? If not, it was cut.

## 1. One trigger (a `devin-fix` label), two transports: pull + push

**Decision:** A single logical trigger — adding the `devin-fix` label to an issue —
reaches the dispatcher two ways: a **pull** scanner that polls the repo for
labeled issues (default), and a **push** webhook that receives GitHub's
`issues.labeled` event. Both call the same `handle_labeled_issue`.

**Why two transports, not "several triggers":** it's still one event (the label)
and one handler; the transports differ only in *who initiates delivery*. The pull
scanner needs no public URL, so the whole system runs and demos from local Docker
— label an issue on GitHub and it's picked up on the next scan. The webhook is the
production "instant" path but requires the service to be publicly reachable and a
hook registered on the repo (which the Devin GitHub App can't self-register). The
pull path removes that setup entirely, which matters for a self-contained demo.

The scanner is idempotent by reusing the same dispatch guard: it lists labeled
issues, skips any with an existing `Run`, and dispatches the rest — so a label
toggled repeatedly, or both transports firing, never spawns a duplicate session.
Gated by `LABEL_SCAN_ENABLED` (set `false` to rely solely on the webhook).

**Why the dispatcher stays transport-agnostic:** it takes a plain issue dict, so
any source (the scanner, the webhook, a cron job, a CLI replay, a Snyk/Dependabot
scan) reuses it with no new core logic.

**Cut:** A second, redundant **GitHub Actions** transport for the same event — it
delivered the same payload to the same handler, so it was extra surface area with
no new capability. The CLI (`scan` / `dispatch`) remains a plain ops/replay tool.

## 2. Verify before opening the PR (the point of the system)

**Decision:** The session must make the fix **and prove it green** (`pre-commit`
on the changed files + relevant tests) before opening a PR, and must emit a
machine-readable `pass`/`fail` verdict.

**Why:** This is the differentiator from Dependabot-style automation, which opens
a PR regardless of whether anything works. Here the verdict is an objective
signal recorded per run, so "did the bot actually fix it?" has a real answer.

**Detail:** The prompt scopes pre-commit to **changed files**
(`pre-commit run --files …`), not `--all-files`. On Superset, `--all-files` scans
thousands of files and can run for many minutes; the changed-file run validates
the same hooks for the edit while keeping sessions fast and reliable. (Note: the
fork's own CI still runs the full suite — that's the maintainer's gate, separate
from the session's self-check.)

## 3. Draft PRs by default

**Decision:** Sessions open **draft** PRs (`draft_pr=True`).

**Why:** Human-in-the-loop by design. The automation's job is to produce a
verified fix and surface it; a person owns the decision to request review and
merge. Drafts also don't trigger CODEOWNERS / required-reviewer auto-requests or
enter merge queues, so opening several at once won't spam maintainers. CI still
runs on drafts, so reviewers get full signal. Flipping `draft_pr=False` switches
to ready-for-review PRs with no other change.

## 4. Session lifecycle is decoupled from PR lifecycle

**Decision:** A session is **not** terminated when its PR opens. The orchestrator
models `pr_open` as a distinct, **non-terminal** state.

**Why:** Opening the PR is the deliverable, not the exit condition. The session
stays reachable to answer review comments and fix CI failures. It becomes
terminal only when it explicitly finishes, is terminated, or auto-suspends after
inactivity. This is why `pr_open` is the normal resting state, and why
Verdict/Duration/Cost stay blank for those runs until termination — by design,
not a bug.

## 5. Structured-output verdict + defensive parsing (not schema enforcement)

**Decision:** The prompt asks for an exact `structured_output` JSON shape
(`verdict`, `pr_url`, `checks`, `summary`); the poller parses it **defensively**.

**Why:** The verdict is the objective pass/fail signal. But the pipeline must not
break on a missing/garbled field, so the poller falls back gracefully: no verdict
→ judge by whether a PR exists.

**Cut:** API-level `structured_output_schema` enforcement. The prompt + defensive
poller already yield a clean verdict; adding API validation was belt-and-braces
that didn't move the reliability needle for a 3-issue workflow. Also **cut**
`max_acu_limit` (a per-session cost cap) — unneeded for this scope.

## 6. Stall self-healing: one-shot nudge + visible "stalled"

**Decision:** When a `running`, PR-less run's heartbeat is older than
`STALL_SECONDS` (360s) and it hasn't been nudged, the poller sends **one** message
asking it to continue; `nudged_at` ensures it's one-shot. If still stale, it's
flagged as **stalled / needs attention**.

**Why:** This was the key reliability lesson. Sessions work "heads-down" and emit
few events, so `updated_at` and `acus_consumed` freeze during real work — a
healthy session can look dead, and a genuinely hung one is indistinguishable.
A bounded, idempotent nudge recovers silent hangs (it provably did) **without**
masking real problems, because anything still stale afterward is surfaced, not
hidden.

## 7. PR-by-branch fallback

**Decision:** If a session hasn't surfaced its PR in `structured_output` /
`pull_requests` yet, the poller looks it up by the deterministic branch
`devin/fix-issue-<n>` on GitHub.

**Why:** Observability shouldn't lag the real world. The session object can be
slow to expose the PR (e.g. while paused), but the PR already exists on GitHub;
the fallback shows it on the dashboard the moment it's real.

## 8. Observability surfaces

**Decision:** Ship a live `/dashboard`, `/api/metrics` + `/api/runs` JSON,
`summary.md`, and issue comments — all reading from one SQLite store. Track Devin
`status_detail` and a last-update **heartbeat** per run.

**Why:** Knowing the system is working needs more than logs.
`status_detail` ("working" vs "waiting_for_user" vs "waiting_for_approval")
distinguishes healthy progress from blocked-on-input, and the heartbeat exposes
staleness at a glance. Issue comments keep the audit trail on the work item.

## 9. Headline metric: remediation rate, not raw success rate

**Decision:** The primary headline is **Remediation rate** (`pr_count/total`),
with **Verified pass** (`succeeded/terminal`) as a secondary metric.

**Why:** For a human-in-the-loop bot, an open verified **draft PR is the success
outcome** — explicit `pass`/`fail` verdicts only finalize on termination, which
may never happen if a session sits awaiting review. Leading with raw success rate
showed a misleading "0%" next to three open PRs. Remediation rate reflects the
real outcome: an issue produced a verified PR.

## 10. SQLite + single process

**Decision:** One SQLite file, one process, in-process asyncio poller.

**Why:** Matches "simple but reliable" and the scale (a handful of issues). No
broker, no external DB, trivial to run in one container and inspect. Trade-off:
single-instance only (dispatch idempotency and state are local); horizontal
scaling would require a shared DB. Documented rather than over-engineered.

**Schema management:** the table is created with SQLAlchemy `create_all` at
startup — no migration framework. This is intentional for the scope: a fresh
volume always gets the current schema. The caveat is that adding a column later
does **not** ALTER a pre-existing database file; on the rare schema change you
recreate the volume (run history is observability data, not a system of record).
A tool like Alembic would be the upgrade path if the store ever needed to
preserve history across schema changes.

## 11. Idempotency

**Decision:** `issue_number` is unique; a repeated label event returns the
existing run instead of spawning a duplicate session (explicit `force` to re-run).

**Why:** GitHub redelivers webhooks and users re-toggle labels; the system must
not burn a second session (and ACUs) for the same issue by accident.

## 12. Security

**Decision:** Webhook payloads are HMAC-verified (`X-Hub-Signature-256`); all
credentials come from the environment and are never logged.

**Why:** The webhook is a public entry point that spends real ACUs; it must
reject forged deliveries.

## Known limitations (honest caveats)

- **ACUs / explicit verdict may stay 0 / null** for sessions that open a PR and
  never terminate — that telemetry finalizes on termination, and these sessions
  stay open awaiting review. The offline demo proves the columns populate when a
  session does terminate.
- **GitHub App installation tokens expire hourly**, so unattended issue comments
  fail after an hour; a long-lived PAT fixes this. PR discovery via the Devin API
  is unaffected.
- **Single instance** by design (local SQLite); multi-replica needs a shared DB.
- **The Devin GitHub App can't register webhooks** (missing admin permission).
  This is why the **pull scanner is the default** — it needs no hook and no public
  URL. Register a hook manually only if you want the instant push path.
- **The label scanner polls** (default 60s), so the pull path adds up-to-one-
  interval of latency vs. the push webhook — a deliberate trade of immediacy for
  zero-setup. Both share the same idempotent dispatch, so running both is safe.
