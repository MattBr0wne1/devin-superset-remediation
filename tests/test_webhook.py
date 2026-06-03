"""End-to-end webhook tests using FastAPI's TestClient with injected fakes."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.security import verify_github_signature


def _client(settings, session_factory, fake_devin, fake_github):
    app = create_app(settings, devin=fake_devin, github=fake_github,
                     session_factory=session_factory, start_poller=False)
    return TestClient(app)


def _issue_payload(number=5, label="devin-fix", action="labeled"):
    return {
        "action": action,
        "label": {"name": label},
        "issue": {"number": number, "title": "Fix utcnow", "body": "do it"},
    }


def test_healthz(settings, session_factory, fake_devin, fake_github):
    client = _client(settings, session_factory, fake_devin, fake_github)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["repo"] == "MattBr0wne1/superset"


def test_webhook_dispatches_on_trigger_label(settings, session_factory, fake_devin, fake_github):
    client = _client(settings, session_factory, fake_devin, fake_github)
    r = client.post("/webhooks/github-issue", json=_issue_payload(number=5),
                    headers={"X-GitHub-Event": "issues"})
    assert r.status_code == 202
    body = r.json()
    assert body["run"]["issue_number"] == 5
    assert len(fake_devin.created) == 1


def test_webhook_ignores_other_labels(settings, session_factory, fake_devin, fake_github):
    client = _client(settings, session_factory, fake_devin, fake_github)
    r = client.post("/webhooks/github-issue", json=_issue_payload(label="bug"),
                    headers={"X-GitHub-Event": "issues"})
    assert r.status_code == 200
    assert "ignored" in r.json()
    assert len(fake_devin.created) == 0


def test_webhook_rejects_bad_signature(settings, session_factory, fake_devin, fake_github):
    settings.webhook_secret = "topsecret"
    client = _client(settings, session_factory, fake_devin, fake_github)
    r = client.post("/webhooks/github-issue", json=_issue_payload(),
                    headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401


def test_webhook_accepts_valid_signature(settings, session_factory, fake_devin, fake_github):
    settings.webhook_secret = "topsecret"
    client = _client(settings, session_factory, fake_devin, fake_github)
    payload = _issue_payload(number=11)
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/github-issue", content=body,
                    headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig,
                             "Content-Type": "application/json"})
    assert r.status_code == 202


def test_metrics_and_runs_endpoints(settings, session_factory, fake_devin, fake_github):
    client = _client(settings, session_factory, fake_devin, fake_github)
    client.post("/webhooks/github-issue", json=_issue_payload(number=5),
                headers={"X-GitHub-Event": "issues"})
    assert client.get("/api/runs").json()["items"][0]["issue_number"] == 5
    assert client.get("/api/metrics").json()["total_runs"] == 1
    assert client.get("/dashboard").status_code == 200


def test_verify_signature_skips_when_no_secret():
    assert verify_github_signature(b"x", None, "") is True
    assert verify_github_signature(b"x", "sha256=bad", "s") is False
