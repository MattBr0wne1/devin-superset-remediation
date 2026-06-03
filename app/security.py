"""Webhook signature verification helpers."""

from __future__ import annotations

import hashlib
import hmac


def verify_github_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Validate a GitHub ``X-Hub-Signature-256`` header against the raw body.

    If no secret is configured, verification is skipped (returns True) so the
    service can run in local/dev mode — production deployments must set one.
    """
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
