"""Shared fixtures.

Database tests use a *dedicated* PostgreSQL test database configured only via
``TEST_PG*`` variables (never the application's PG* settings or ``.env``).
As a safety guard the database name must end in ``_test``; the
``research_hive`` schema inside it is dropped and re-created once per session.
Tests that need the database are skipped when it is not configured.

No test calls a paid API: demo providers or in-process fakes are used.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import Settings
from app.db.engine import Database, create_sync_engine_for
from app.main import build_services, create_app
from app.services.events import EventBroker, EventRecorder
from app.services.runs import ProjectService, RunService

BACKEND = Path(__file__).resolve().parents[1]
TABLES = (
    "run_events",
    "report_citations",
    "reports",
    "findings",
    "sources",
    "agent_tasks",
    "research_runs",
    "projects",
)
MUTATION_HEADERS = {"X-Research-Hive": "1", "Content-Type": "application/json"}


def _test_db_configured() -> bool:
    return bool(os.environ.get("TEST_PGDATABASE"))


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests: never reads .env, demo mode, no artificial delays."""
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "demo_mode": True,
        "demo_step_delay_ms": 0,
        "allowed_hosts": "localhost,127.0.0.1,testserver",
        "app_origin": "http://localhost:5173",
        "log_level": "WARNING",
        "pghost": os.environ.get("TEST_PGHOST", "127.0.0.1"),
        "pgport": int(os.environ.get("TEST_PGPORT", "5432")),
        "pgdatabase": os.environ.get("TEST_PGDATABASE", "research_hive_test"),
        "pguser": os.environ.get("TEST_PGUSER", "postgres"),
        "pgpassword": SecretStr(os.environ.get("TEST_PGPASSWORD", "")),
        "pgsslmode": os.environ.get("TEST_PGSSLMODE", "disable"),
        "pgsslrootcert": os.environ.get("TEST_PGSSLROOTCERT") or None,
        "anthropic_api_key": None,
        "tavily_api_key": None,
        "anthropic_model": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[Settings]:
    """Reset the test schema and apply all migrations once per session."""
    if not _test_db_configured():
        pytest.skip("TEST_PGDATABASE is not set; see docs/testing.md")
    settings = make_settings()
    if not settings.pgdatabase.endswith("_test"):
        pytest.exit("Refusing to run: TEST_PGDATABASE must end with '_test'.", returncode=2)
    engine = create_sync_engine_for(settings)
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{settings.db_schema}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{settings.db_schema}"'))
    engine.dispose()
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    yield settings


@pytest.fixture
async def db(migrated_database: Settings) -> AsyncIterator[Database]:
    database = Database(migrated_database)
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE "
                + ", ".join(f'"{migrated_database.db_schema}".{t}' for t in TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        )
    yield database
    await database.dispose()


@pytest.fixture
def broker() -> EventBroker:
    return EventBroker()


@pytest.fixture
def run_service(db: Database, broker: EventBroker) -> RunService:
    return RunService(db, db.settings, EventRecorder(db, broker))


@pytest.fixture
async def project(db: Database) -> Any:
    return await ProjectService(db).create("Test project", "")


async def queue_run(
    run_service: RunService,
    project_id: uuid.UUID,
    *,
    question: str = "Should our city fund e-bike vouchers?",
    depth: str = "standard",
    input_urls: list[str] | None = None,
) -> Any:
    run, _ = await run_service.create(
        project_id,
        question=question,
        context=None,
        input_urls=input_urls or [],
        depth=depth,
        idempotency_key=f"test-{uuid.uuid4()}",
    )
    return run


@pytest.fixture
async def client(migrated_database: Settings, db: Database) -> AsyncIterator[httpx.AsyncClient]:
    """API client without the background worker (tests drive execution)."""
    app = create_app(migrated_database, start_worker=False)
    services = build_services(migrated_database, start_worker=False)
    await services.db.dispose()
    services.db = db
    app.state.services = services
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
