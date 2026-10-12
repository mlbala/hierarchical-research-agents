"""Typed application exceptions and the consistent API error envelope.

Every error that reaches the browser has the shape::

    {"error": {"code": "...", "message": "...", "request_id": "...", "retryable": false}}

``message`` is always a *safe* message written by us; raw exception text,
stack traces and provider responses are never exposed to the client.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base class for expected, classified failures."""

    code: str = "APP_ERROR"
    http_status: int = 500
    retryable: bool = False
    safe_message: str = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.safe_message = message or self.safe_message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        if http_status is not None:
            self.http_status = http_status
        self.details = details
        super().__init__(self.safe_message)


class ConfigurationError(AppError):
    """Required configuration is missing or invalid (never includes values)."""

    code = "CONFIGURATION_ERROR"
    http_status = 503
    safe_message = "The server is not configured for this operation."


class ProviderError(AppError):
    """An external provider (LLM or search) failed.

    ``retry_after`` carries provider retry guidance in seconds when available.
    ``usage`` lets LLM failures still report the tokens they consumed.
    """

    code = "PROVIDER_ERROR"
    http_status = 502
    safe_message = "An external provider failed."

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str,
        code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        usage: Any | None = None,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)
        self.provider = provider
        self.retry_after = retry_after
        self.usage = usage


class ResearchTimeoutError(AppError):
    code = "RUN_TIMEOUT"
    http_status = 504
    safe_message = "The research run exceeded its time limit."


class UnsafeUrlError(AppError):
    """A URL was rejected by the SSRF guard. Never retried."""

    code = "UNSAFE_URL"
    http_status = 400
    safe_message = "The URL is not a permitted public web address."


class BudgetExceededError(AppError):
    code = "BUDGET_EXCEEDED"
    http_status = 429
    safe_message = "The run reached one of its execution limits."

    def __init__(self, resource: str, limit: int) -> None:
        super().__init__(f"The run reached its {resource} limit ({limit}).")
        self.resource = resource
        self.limit = limit


class DatabaseUnavailableError(AppError):
    code = "DATABASE_UNAVAILABLE"
    http_status = 503
    retryable = True
    safe_message = "The database is temporarily unavailable."


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = 404
    safe_message = "The requested resource was not found."


class ConflictError(AppError):
    code = "CONFLICT"
    http_status = 409
    safe_message = "The request conflicts with the current state."


class InvalidStateError(ConflictError):
    code = "INVALID_STATE"
    safe_message = "That action is not allowed in the resource's current state."


class RequestRejectedError(AppError):
    """Generic 4xx produced by validation or the security middleware."""

    code = "BAD_REQUEST"
    http_status = 400
    safe_message = "The request was rejected."


class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    http_status = 429
    retryable = True
    safe_message = "Too many requests. Please wait and try again."


class ToolPermissionError(AppError):
    """An agent role tried to use a tool it is not permitted to use."""

    code = "TOOL_NOT_PERMITTED"
    http_status = 403
    safe_message = "This agent is not permitted to use that tool."


class NoUsableEvidenceError(AppError):
    code = "NO_USABLE_EVIDENCE"
    http_status = 422
    safe_message = (
        "No usable evidence was found, so no report was produced. "
        "Try rephrasing the question or adding reference URLs."
    )


class CitationValidationError(AppError):
    code = "CITATION_VALIDATION_FAILED"
    http_status = 422
    safe_message = "The draft report failed citation validation."


class RunCancelledError(AppError):
    """Raised inside a run when cancellation was requested."""

    code = "RUN_CANCELLED"
    http_status = 409
    safe_message = "The run was cancelled."


# ---------------------------------------------------------------------------
# Error envelope and centralized handlers
# ---------------------------------------------------------------------------


def request_id_of(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def error_response(
    *,
    status: int,
    code: str,
    message: str,
    request_id: str,
    retryable: bool = False,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "retryable": retryable,
    }
    if details:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def is_database_unavailable(exc: BaseException) -> bool:
    """True for connection-level database failures (not constraint errors)."""
    if isinstance(exc, DatabaseUnavailableError | PoolTimeoutError | InterfaceError):
        return True
    if isinstance(exc, OperationalError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so every failure uses the same safe envelope."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": "60"}
        return error_response(
            status=exc.http_status,
            code=exc.code,
            message=exc.safe_message,
            request_id=request_id_of(request),
            retryable=exc.retryable,
            details=exc.details,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Report field locations and messages only; never echo submitted input.
        details = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "message": str(err.get("msg", "Invalid value")),
            }
            for err in exc.errors()
        ][:20]
        return error_response(
            status=422,
            code="VALIDATION_ERROR",
            message="Some fields are invalid.",
            request_id=request_id_of(request),
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        message = (
            "The requested resource was not found."
            if exc.status_code == 404
            else ("The request could not be processed.")
        )
        return error_response(
            status=exc.status_code,
            code=code,
            message=message,
            request_id=request_id_of(request),
        )


def unexpected_error_response(exc: Exception, *, request_id: str, path: str) -> JSONResponse:
    """Convert an unhandled exception into a safe 500/503 envelope.

    Called from the security middleware rather than registered as a Starlette
    ``Exception`` handler: Starlette re-raises after running such handlers,
    which would make uvicorn log the same failure a second time.
    """
    if is_database_unavailable(exc):
        log.warning("database.unavailable", error_type=type(exc).__name__, path=path)
        return error_response(
            status=503,
            code=DatabaseUnavailableError.code,
            message=DatabaseUnavailableError.safe_message,
            request_id=request_id,
            retryable=True,
        )
    # Logged exactly once here; the redaction processor scrubs secrets from
    # the formatted traceback before it is written.
    log.error(
        "request.unhandled_exception",
        error_type=type(exc).__name__,
        path=path,
        exc_info=exc,
    )
    return error_response(
        status=500,
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
        request_id=request_id,
    )
