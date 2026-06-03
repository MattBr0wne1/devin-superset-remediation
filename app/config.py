"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the remediation orchestrator.

    Values are read from environment variables (or a local ``.env`` file).
    Secrets must never be hard-coded; supply them via the environment.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Devin API ---
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_base_url: str = "https://api.devin.ai/v3"
    # Optional: attribute created sessions to a human user (requires the service
    # user to hold the ImpersonateOrgSessions permission).
    create_as_user_id: str | None = None

    # --- GitHub ---
    github_token: str = ""
    # The fork that owns the issues and receives remediation PRs.
    repo: str = "MattBr0wne1/superset"
    # Label that triggers a remediation run.
    trigger_label: str = "devin-fix"
    # Shared secret used to verify GitHub webhook signatures (X-Hub-Signature-256).
    webhook_secret: str = ""

    # --- Storage / behaviour ---
    db_path: str = "remediation.db"
    poll_interval_seconds: int = 20
    summary_path: str = "summary.md"
    # Open PRs as drafts for human review (instruction passed to the session).
    draft_pr: bool = True

    @property
    def sessions_url(self) -> str:
        return f"{self.devin_base_url}/organizations/{self.devin_org_id}/sessions"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
