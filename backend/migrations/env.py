"""Alembic environment.

Connection parameters come from the application ``Settings`` (PG* variables),
or from ``config.attributes["settings"]`` when migrations are invoked
programmatically (the test suite and scripts/bootstrap_db.py do this).

All tables, including ``alembic_version``, live in the application schema.
Migrations never create databases, roles or extensions. The schema itself must
already exist (scripts/bootstrap_db.py creates it when permitted).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.engine import create_sync_engine_for
from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _settings() -> Settings:
    provided = config.attributes.get("settings")
    return provided if isinstance(provided, Settings) else get_settings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without connecting."""
    settings = _settings()
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=settings.db_schema,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = _settings()
    engine = create_sync_engine_for(settings)
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
            {"schema": settings.db_schema},
        ).scalar()
        # SQLAlchemy 2.x autobegins a transaction for the query above; end it
        # so Alembic owns (and commits) the migration transactions.
        connection.commit()
        if not exists:
            raise SystemExit(
                f"Schema '{settings.db_schema}' does not exist. Run scripts/bootstrap_db.py "
                "or ask a database administrator to create it."
            )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=settings.db_schema,
            include_schemas=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
