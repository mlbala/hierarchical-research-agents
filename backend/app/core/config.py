"""Application settings.

All configuration is read from environment variables (optionally via the
repository-root ``.env`` file). Secrets are typed as ``SecretStr`` so they are
never rendered by ``repr`` or accidentally serialised into API responses,
run snapshots, or logs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> repository root is three levels above ``app``.
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"

Depth = Literal["quick", "standard"]
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Models known to accept the server-side ``fallbacks: "default"`` refusal
# fallback (see Anthropic's refusal/fallback documentation). For any other
# configured model the parameter is simply not sent.
REFUSAL_FALLBACK_MODELS = frozenset(
    {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
)


@dataclass(frozen=True)
class RunLimits:
    """Hard execution limits for a single research run.

    These are enforced in code (``RunBudget`` and graph routing), never only in
    prompts. Quick depth derives tighter limits from the configured maxima.
    """

    max_llm_calls: int
    max_search_calls: int
    max_fetch_calls: int
    max_sources: int
    research_retries: int
    writing_revisions: int
    deadline_seconds: int
    queries_per_researcher: int
    pages_per_researcher: int
    max_findings_per_researcher: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class Settings(BaseSettings):
    """Typed, validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- HTTP server -----------------------------------------------------
    app_env: Literal["development", "production", "test"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    # Comma-separated list of browser origins allowed to call the API.
    app_origin: str = "http://localhost:5173"
    allowed_hosts: str = "localhost,127.0.0.1"
    # Comma-separated proxy IPs whose X-Forwarded-For header may be trusted.
    # Empty (the default) means forwarded headers are ignored entirely.
    trusted_proxies: str = ""
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    # --- Execution mode ----------------------------------------------------
    demo_mode: bool = True
    # Artificial pause between demo steps so the UI shows progress (0 in tests).
    demo_step_delay_ms: int = Field(default=600, ge=0, le=10_000)

    # --- PostgreSQL ----------------------------------------------------------
    pghost: str = "localhost"
    pgport: int = Field(default=5432, ge=1, le=65535)
    pgdatabase: str = "research_hive"
    pguser: str = "postgres"
    pgpassword: SecretStr = SecretStr("")
    pgsslmode: SslMode = "verify-full"
    pgsslrootcert: str | None = None
    db_schema: str = "research_hive"
    db_pool_size: int = Field(default=3, ge=1, le=20)
    db_max_overflow: int = Field(default=2, ge=0, le=20)
    db_pool_timeout_seconds: int = Field(default=10, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)

    # --- Providers -----------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    # Optional ``output_config.effort`` (low|medium|high|xhigh|max). Empty means
    # "use the model default"; some older models reject the parameter.
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    anthropic_refusal_fallback: bool = True
    tavily_api_key: SecretStr | None = None

    # Optional pricing used only to *estimate* LLM cost. When either value is
    # missing the cost is reported as unknown, never as zero.
    llm_input_usd_per_mtok: float | None = Field(default=None, ge=0)
    llm_output_usd_per_mtok: float | None = Field(default=None, ge=0)

    # --- Logging -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Concurrency and admission ------------------------------------------
    max_concurrent_runs: int = Field(default=1, ge=1, le=4)
    max_parallel_agent_tasks: int = Field(default=2, ge=1, le=4)
    max_queued_runs: int = Field(default=10, ge=1, le=100)
    run_create_rate_limit_per_minute: int = Field(default=6, ge=1, le=120)

    # --- Per-run budgets (standard depth) -------------------------------------
    max_llm_calls_per_run: int = Field(default=20, ge=10, le=100)
    max_search_calls_per_run: int = Field(default=12, ge=1, le=100)
    max_fetch_calls_per_run: int = Field(default=24, ge=0, le=200)
    max_sources_per_run: int = Field(default=20, ge=1, le=200)
    max_research_retries: int = Field(default=1, ge=0, le=1)
    max_writing_revisions: int = Field(default=1, ge=0, le=1)
    run_timeout_seconds: int = Field(default=600, ge=30, le=3600)

    # --- Per-operation limits -------------------------------------------------
    llm_timeout_seconds: float = Field(default=60, ge=5, le=600)
    fetch_timeout_seconds: float = Field(default=15, ge=1, le=60)
    search_timeout_seconds: float = Field(default=20, ge=1, le=60)
    fetch_max_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
    source_max_chars: int = Field(default=30_000, ge=1_000, le=200_000)
    transient_retries: int = Field(default=2, ge=0, le=5)
    # Bounds on what is sent to / accepted from the model.
    llm_max_prompt_chars: int = Field(default=60_000, ge=5_000, le=400_000)
    llm_source_excerpt_chars: int = Field(default=4_000, ge=500, le=30_000)
    llm_max_output_tokens: int = Field(default=16_000, ge=1_024, le=64_000)

    @field_validator("db_schema")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        # The schema name is interpolated into DDL by the bootstrap script, so
        # only plain lower-case identifiers are accepted.
        if not _IDENTIFIER.match(value):
            raise ValueError("DB_SCHEMA must be a lower-case SQL identifier")
        return value

    @field_validator("anthropic_model", "pgsslrootcert", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("anthropic_api_key", "tavily_api_key", "anthropic_effort", mode="before")
    @classmethod
    def _blank_secret_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    # --- Derived values -------------------------------------------------------

    @property
    def execution_mode(self) -> Literal["live", "demo"]:
        return "demo" if self.demo_mode else "live"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def allowed_origin_list(self) -> list[str]:
        """Explicit browser origins (never a wildcard).

        Includes the configured frontend origin(s) plus the backend's own
        origin for every allowed host, which is what the browser sends when the
        built frontend is served by FastAPI itself.
        """
        origins = {o.strip().rstrip("/") for o in self.app_origin.split(",") if o.strip()}
        for host in self.allowed_host_list:
            if host != "*":
                origins.add(f"http://{host}:{self.port}")
        return sorted(origins)

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]

    @property
    def sslrootcert_path(self) -> Path | None:
        """CA bundle path, resolved relative to the repository root."""
        if not self.pgsslrootcert:
            return None
        path = Path(self.pgsslrootcert).expanduser()
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()

    @property
    def refusal_fallback_enabled(self) -> bool:
        return bool(
            self.anthropic_refusal_fallback and self.anthropic_model in REFUSAL_FALLBACK_MODELS
        )

    def limits_for(self, depth: Depth) -> RunLimits:
        """Return the hard limits for a run of the given depth."""
        if depth == "quick":
            return RunLimits(
                max_llm_calls=min(12, self.max_llm_calls_per_run),
                max_search_calls=min(4, self.max_search_calls_per_run),
                max_fetch_calls=min(8, self.max_fetch_calls_per_run),
                max_sources=min(10, self.max_sources_per_run),
                research_retries=0,
                writing_revisions=0,
                deadline_seconds=min(300, self.run_timeout_seconds),
                queries_per_researcher=2,
                pages_per_researcher=3,
                max_findings_per_researcher=6,
            )
        return RunLimits(
            max_llm_calls=self.max_llm_calls_per_run,
            max_search_calls=self.max_search_calls_per_run,
            max_fetch_calls=self.max_fetch_calls_per_run,
            max_sources=self.max_sources_per_run,
            research_retries=self.max_research_retries,
            writing_revisions=self.max_writing_revisions,
            deadline_seconds=self.run_timeout_seconds,
            queries_per_researcher=3,
            pages_per_researcher=5,
            max_findings_per_researcher=10,
        )

    def settings_snapshot(self, depth: Depth) -> dict[str, Any]:
        """Non-secret settings recorded on each run for later inspection."""
        return {
            "execution_mode": self.execution_mode,
            "depth": depth,
            "limits": self.limits_for(depth).as_dict(),
            "llm_provider": "demo" if self.demo_mode else "anthropic",
            "llm_model": "demo-fixtures" if self.demo_mode else self.anthropic_model,
            "llm_effort": None if self.demo_mode else self.anthropic_effort,
            "search_provider": "demo" if self.demo_mode else "tavily",
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "fetch_timeout_seconds": self.fetch_timeout_seconds,
            "fetch_max_bytes": self.fetch_max_bytes,
            "source_max_chars": self.source_max_chars,
            "transient_retries": self.transient_retries,
            "max_parallel_agent_tasks": self.max_parallel_agent_tasks,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (tests construct ``Settings`` directly)."""
    return Settings()
