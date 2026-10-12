"""Database engine, sessions and connection parameters.

Connections are built from structured ``PG*`` parameters with
``sqlalchemy.URL.create`` (never string concatenation), so passwords with
special characters work and are never embedded in a loggable URL string.
TLS options are passed straight to libpq; ``verify-full`` with an explicit CA
bundle is the default for Aiven.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.errors import DatabaseUnavailableError
from app.db.models import SCHEMA


def libpq_params(settings: Settings) -> dict[str, Any]:
    """TLS and session parameters shared by every connection."""
    params: dict[str, Any] = {
        "sslmode": settings.pgsslmode,
        "connect_timeout": 10,
        "application_name": "research-hive",
    }
    if settings.db_statement_timeout_ms:
        # Guard against runaway queries; transactions are kept short anyway.
        params["options"] = f"-c statement_timeout={settings.db_statement_timeout_ms}"
    cert = settings.sslrootcert_path
    if cert is not None:
        params["sslrootcert"] = str(cert)
    return params


def build_url(settings: Settings, *, driver: str = "postgresql+psycopg") -> URL:
    return URL.create(
        drivername=driver,
        username=settings.pguser,
        password=settings.pgpassword.get_secret_value() or None,
        host=settings.pghost,
        port=settings.pgport,
        database=settings.pgdatabase,
    )


def _execution_options(settings: Settings) -> dict[str, Any]:
    # Models are declared in the logical ``research_hive`` schema; map it to
    # the configured schema when an operator chose a different name.
    if settings.db_schema == SCHEMA:
        return {}
    return {"schema_translate_map": {SCHEMA: settings.db_schema}}


def create_engine_for(settings: Settings) -> AsyncEngine:
    """Small async pool sized for a small managed PostgreSQL service."""
    return create_async_engine(
        build_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=libpq_params(settings),
        execution_options=_execution_options(settings),
    )


def create_sync_engine_for(settings: Settings) -> Engine:
    """Synchronous engine used by Alembic migrations and the bootstrap script."""
    return create_engine(
        build_url(settings),
        poolclass=NullPool,
        connect_args=libpq_params(settings),
        execution_options=_execution_options(settings),
    )


async def open_raw_connection(settings: Settings) -> psycopg.AsyncConnection[Any]:
    """Dedicated autocommit connection (used to hold the worker advisory lock)."""
    return await psycopg.AsyncConnection.connect(
        host=settings.pghost,
        port=settings.pgport,
        dbname=settings.pgdatabase,
        user=settings.pguser,
        password=settings.pgpassword.get_secret_value() or None,
        autocommit=True,
        **libpq_params(settings),
    )


class Database:
    """Owns the engine and hands out short-lived transactional sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine_for(settings)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """One transaction per ``async with``; commits on success.

        Callers must never hold a session open across an LLM or network call.
        Connection-level failures are translated to ``DatabaseUnavailableError``.
        """
        try:
            async with self._sessionmaker() as session, session.begin():
                yield session
        except (OperationalError, InterfaceError, PoolTimeoutError) as exc:
            raise DatabaseUnavailableError() from exc
        except DBAPIError as exc:
            if exc.connection_invalidated:
                raise DatabaseUnavailableError() from exc
            raise

    async def dispose(self) -> None:
        await self.engine.dispose()
