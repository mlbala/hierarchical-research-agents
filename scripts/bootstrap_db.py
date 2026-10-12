#!/usr/bin/env python3
"""Prepare the Research Hive database schema.

Usage (from the repository root)::

    cd backend && uv run python ../scripts/bootstrap_db.py [--check]

What it does, in order:

1. Connects with the PG* settings from ``.env`` / the environment, using the
   configured TLS mode (``verify-full`` + CA bundle by default) and reports the
   server version and whether the connection is encrypted.
2. Creates the application schema (DB_SCHEMA, default ``research_hive``) if it
   is missing *and* the credentials are allowed to. It never creates
   databases, roles or extensions, and never drops anything.
3. Runs ``alembic upgrade head``.

``--check`` performs step 1 and reports pending migrations without changing
anything. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.engine import create_sync_engine_for  # noqa: E402


def _alembic_config(settings: Settings) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    config.attributes["settings"] = settings
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report status; change nothing")
    args = parser.parse_args()

    settings = Settings()
    schema = settings.db_schema
    print(
        f"Target: host={settings.pghost} port={settings.pgport} "
        f"database={settings.pgdatabase} user={settings.pguser} schema={schema}"
    )
    print(
        f"TLS: sslmode={settings.pgsslmode} "
        f"sslrootcert={'set' if settings.sslrootcert_path else 'not set'}"
    )
    if settings.pgsslmode not in ("verify-ca", "verify-full"):
        print(
            "WARNING: server certificate verification is disabled for this connection. "
            "Use PGSSLMODE=verify-full with PGSSLROOTCERT for Aiven or any remote server."
        )
    cert = settings.sslrootcert_path
    if cert is not None and not cert.is_file():
        print(f"ERROR: PGSSLROOTCERT points to a missing file: {cert}")
        return 2

    engine = create_sync_engine_for(settings)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar()
            ssl = conn.execute(
                text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            ).scalar()
            print(f"Connected: PostgreSQL {version}; encrypted={bool(ssl)}")
            exists = conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}
            ).scalar()
            script = ScriptDirectory.from_config(_alembic_config(settings))
            head = script.get_current_head()
            current = None
            if exists:
                ctx = MigrationContext.configure(conn, opts={"version_table_schema": schema})
                current = ctx.get_current_revision()
            print(
                f"Schema '{schema}': {'present' if exists else 'missing'}; "
                f"migration revision: {current or 'none'} (head: {head})"
            )

            if args.check:
                return 0 if current == head else 1

            if not exists:
                # Identifier was validated by Settings (lower-case SQL identifier).
                try:
                    conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                    conn.commit()
                    print(f"Created schema '{schema}'.")
                except SQLAlchemyError:
                    print(
                        f"ERROR: could not create schema '{schema}'. Ask an administrator to run:\n"
                        f'  CREATE SCHEMA "{schema}" AUTHORIZATION <migration_user>;'
                    )
                    return 3
    except SQLAlchemyError as exc:
        # Show only the exception class: driver messages can include hostnames
        # and usernames, which belong in operator logs rather than terminals
        # that might be shared.
        print(
            f"ERROR: database connection failed ({type(exc).__name__}). "
            "Check PG* settings, network access and the CA certificate."
        )
        return 2
    finally:
        engine.dispose()

    command.upgrade(_alembic_config(settings), "head")
    print("Migrations applied: database is at head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
