"""Configuration readiness.

Reports *which* settings are missing or unsafe, never their values, and never
calls a paid provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class ConfigIssue:
    key: str
    severity: Literal["error", "warning"]
    message: str


def configuration_issues(settings: Settings) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    if not settings.demo_mode:
        if settings.anthropic_api_key is None:
            issues.append(ConfigIssue("ANTHROPIC_API_KEY", "error", "Required for live research."))
        if not settings.anthropic_model:
            issues.append(
                ConfigIssue(
                    "ANTHROPIC_MODEL",
                    "error",
                    "Required for live research (for example claude-opus-5-5).",
                )
            )
        if settings.tavily_api_key is None:
            issues.append(ConfigIssue("TAVILY_API_KEY", "error", "Required for live web search."))
    remote_db = settings.pghost.lower() not in _LOCAL_HOSTS
    cert = settings.sslrootcert_path
    if settings.pgsslmode in ("verify-ca", "verify-full"):
        if cert is None and remote_db:
            issues.append(
                ConfigIssue(
                    "PGSSLROOTCERT",
                    "warning",
                    "No CA certificate configured; verification will use the system store.",
                )
            )
        elif cert is not None and not cert.is_file():
            issues.append(
                ConfigIssue(
                    "PGSSLROOTCERT", "error", "The configured CA certificate file is missing."
                )
            )
    elif remote_db:
        issues.append(
            ConfigIssue(
                "PGSSLMODE",
                "warning",
                "Database TLS certificate verification is disabled; use verify-full.",
            )
        )
    return issues


def live_ready(settings: Settings) -> bool:
    keys = {"ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "TAVILY_API_KEY"}
    return not any(
        i.key in keys
        for i in configuration_issues(settings.model_copy(update={"demo_mode": False}))
    )
