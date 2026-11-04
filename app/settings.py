from __future__ import annotations

import os
import shlex
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Where the Docker image pre-installs PayPal's MCP server (see Dockerfile).
BUNDLED_PAYPAL_MCP = Path("/opt/paypal-mcp/node_modules/@paypal/mcp/dist/index.js")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"

    # Where the app is reachable from the outside world. Used to build the
    # client-facing quote and payment-plan links (`/q/{token}`, `/p/{token}`).
    public_base_url: str = "http://localhost:8000"

    # SQLite by default so a judge can run the app with zero setup; Render and
    # production point this at Postgres. Render hands out `postgres://` URLs,
    # which the validator below rewrites for the asyncpg driver.
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/sayinvoice.db",
        description="Async SQLAlchemy URL.",
    )

    # ── PayPal ──────────────────────────────────────────────────────────────
    # Sandbox REST app credentials (developer.paypal.com → Apps & Credentials).
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    # `sandbox` or `live`. The hackathon runs entirely on sandbox.
    paypal_env: str = "sandbox"
    # The webhook ID PayPal shows after you register
    # `{public_base_url}/api/webhooks/paypal`. Signature verification needs it;
    # while empty, the webhook endpoint refuses every delivery.
    paypal_webhook_id: str = ""
    # Must be the sandbox *Business* account's email: PayPal rejects invoices
    # whose invoicer email is not the merchant behind the REST credentials.
    paypal_merchant_email: str = ""
    # How to launch PayPal's MCP server (Agent Toolkit tools for the agents).
    # The Docker image pre-installs it and overrides this with a plain `node` call.
    paypal_mcp_command: str = "npx -y @paypal/mcp@1.8.1"

    # ── Gemini / ADK ────────────────────────────────────────────────────────
    # AI Studio API key (simplest for local runs). Leave empty and set the
    # Vertex fields instead to run on Vertex AI.
    google_api_key: str = ""
    vertex_project: str = ""
    vertex_location: str = "us-central1"
    # Chosen by scripts/eval_parser.py: 100% of text and 92% of audio cards fully
    # right at ~2s. Thinking levels are per model: gemini-3.8-flash rejects "minimal".
    gemini_model: str = "gemini-3.5-flash"
    # minimal | low | medium | high; empty keeps the model's default. Lower is faster.
    gemini_thinking_level: str = "minimal"
    # One parse is one Gemini call; past this we retry once, then offer "type instead".
    parse_timeout_s: float = 25.0
    # Gemini takes inline audio up to ~20 MB per request; a spoken job is far smaller.
    max_audio_bytes: int = 10 * 1024 * 1024

    # Voice read-back: "browser" (the phone's own voice: instant, free, multilingual) or
    # "kokoro" (KokoroTTS on the server; needs `uv sync --extra tts` and more memory).
    tts_engine: str = "browser"

    # The "simulate N days later" control for demos. Turn off for a real business.
    demo_clock_enabled: bool = True

    # Seconds between reconcile passes (re-check open invoices with PayPal). 0 turns it off.
    reconcile_interval_s: float = 60.0

    # Until onboarding exists, the first workspace is created from these.
    default_currency: str = "GBP"
    business_name: str = "Dave's Plumbing"
    owner_name: str = "Dave Mason"
    # Days until an invoice is due when no date was said; 0 means due on receipt.
    default_due_days: int = 7

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v.removeprefix("postgres://")
        if v.startswith("postgresql://"):
            return "postgresql+asyncpg://" + v.removeprefix("postgresql://")
        return v

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def paypal_base_url(self) -> str:
        if self.paypal_env.lower() == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    def paypal_mcp_argv(self) -> list[str]:
        """The command that starts PayPal's MCP server.

        The configured command wins if its program exists. Otherwise (e.g. a
        local `.env` saying `npx ...` passed to the Docker image, which has
        node but no npx) use the copy bundled in the image.
        """
        argv = shlex.split(self.paypal_mcp_command)
        if argv and shutil.which(argv[0]):
            return argv
        if BUNDLED_PAYPAL_MCP.exists() and shutil.which("node"):
            return ["node", str(BUNDLED_PAYPAL_MCP)]
        return argv or ["npx", "-y", "@paypal/mcp@1.8.1"]

    @property
    def gemini_configured(self) -> bool:
        return bool(self.google_api_key or self.vertex_project)

    def apply_google_env(self) -> None:
        """Expose Gemini credentials through the env vars google-genai / ADK read.

        `setdefault` so an explicit process environment wins over `.env`.
        """
        if self.google_api_key:
            os.environ.setdefault("GOOGLE_API_KEY", self.google_api_key)
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
        elif self.vertex_project:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self.vertex_project)
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", self.vertex_location)
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")


@lru_cache
def get_settings() -> Settings:
    return Settings()
