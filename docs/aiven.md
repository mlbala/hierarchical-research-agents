# Using Aiven for PostgreSQL

Research Hive works with any PostgreSQL 14+; these notes cover Aiven
specifically. The application never creates, resets or overwrites Aiven
resources: you create the service and (optionally) the database yourself.

## 1. Collect the connection details

In the Aiven Console, open your PostgreSQL service. The **Overview** page shows
the connection information: **host**, **port**, **user** (usually
`avnadmin`), **password** and **database** (usually `defaultdb`).

Use the **service** host and port, not a connection pool (PgBouncer) port:
the worker relies on a session-level advisory lock, which transaction-mode
pooling cannot provide.

If your service restricts inbound traffic, add the public IP address of the
machine running Research Hive to the service's allowed IP addresses.

## 2. Download the CA certificate

On the same page, download the **CA certificate** (`ca.pem`) and save it in
the repository as `certs/aiven-ca.pem`:

```bash
mkdir -p certs
mv ~/Downloads/ca.pem certs/aiven-ca.pem
chmod 600 certs/aiven-ca.pem
```

`certs/` and `*.pem` are git-ignored. The CA certificate is not secret, but it
is environment-specific and does not belong in the repository.

## 3. Configure `.env`

```bash
cp .env.example .env
```

```ini
PGHOST=<service-host>.aivencloud.com
PGPORT=<service-port>
PGDATABASE=research_hive        # or defaultdb / any existing database
PGUSER=avnadmin                 # better: a dedicated runtime role, see below
PGPASSWORD=<password>
PGSSLMODE=verify-full
PGSSLROOTCERT=./certs/aiven-ca.pem
DB_SCHEMA=research_hive
```

* `verify-full` makes libpq check that the server certificate is signed by
  Aiven's CA **and** matches `PGHOST`. Verification is never silently
  disabled: if you set a weaker mode for a remote host, readiness and the
  bootstrap script warn loudly.
* Relative `PGSSLROOTCERT` paths are resolved from the repository root.
* The password is passed to the driver as a structured parameter, so special
  characters need no URL encoding.

## 4. Create the database (optional)

A dedicated database is preferred. Create one in the Aiven Console (the
service's **Databases** tab) or with SQL as `avnadmin`:

```sql
CREATE DATABASE research_hive;
```

If you cannot create a database, point `PGDATABASE` at an existing one
(for example `defaultdb`). Every Research Hive table, including
`alembic_version`, lives inside the `research_hive` schema, so nothing else
in that database is touched.

## 5. Create the schema and run migrations

```bash
cd backend
uv sync
uv run python ../scripts/bootstrap_db.py --check   # read-only: TLS, connection, revision
uv run python ../scripts/bootstrap_db.py           # creates the schema if permitted, migrates
```

Expected output includes `Connected: PostgreSQL <version>; encrypted=True`.

Connection success and application readiness are separate checks. The database
named by `PGDATABASE` can already exist while the schema named by `DB_SCHEMA`
and its tables are missing. `--check` only reports this condition; run the
bootstrap command without `--check` to initialize the schema and migrations.
Use the exact database name, including any underscores or hyphens. This project
uses the `PG*` variables above, not `DB_HOST`/`DB_NAME` or `DATABASE_URL`.

## 6. Least-privilege credentials (recommended)

Instead of running the app as `avnadmin`, create a migration role (owns the
schema) and a runtime role (can only select/insert/update rows). The verified
SQL is in [database.md → Least-privilege roles](database.md#least-privilege-roles-recommended).
Run `bootstrap_db.py` with the migrator credentials and put the runtime
credentials in `.env`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `PGSSLROOTCERT points to a missing file` | wrong path; relative paths start at the repository root |
| `certificate verify failed` | wrong CA file, or `PGHOST` is an IP/alias that does not match the certificate |
| connection timeout | your IP is not in the service's allowed IP list, or a firewall blocks the port |
| `Schema 'research_hive' does not exist` from Alembic | run `bootstrap_db.py`, or have an administrator create the schema |
| worker always in `standby` (readiness) | another Research Hive process holds the lock, or you are connected through a pooler |
| errors mentioning startup options | you are behind a pooler; set `DB_STATEMENT_TIMEOUT_MS=0` or use the service port |

A read-only connection check on 2026-10-12 successfully reached Aiven
PostgreSQL 18.6 with `verify-full` and encrypted transport. Schema initialization
was subsequently confirmed working by the project owner. Connection details
and certificates are environment-specific and remain outside this guide.
