# Testing

No test calls a paid API or uses real research records. Demo providers and
in-process fakes stand in for Anthropic, Tavily and the network.

## Test database

Database-backed tests (pytest `db` tests and Playwright) need a **dedicated**
PostgreSQL database whose name ends in `_test`. They read only `TEST_PG*`
variables, never the application's `PG*` settings or `.env`.

```bash
# Any local PostgreSQL 14+ works (Postgres.app, Homebrew, Docker, ...).
createdb research_hive_test

export TEST_PGHOST=127.0.0.1
export TEST_PGPORT=5432
export TEST_PGDATABASE=research_hive_test
export TEST_PGUSER=postgres
export TEST_PGPASSWORD=           # if required
export TEST_PGSSLMODE=disable     # local only; use verify-full for remote servers
```

At the start of each pytest session the `research_hive` schema **in that test
database** is dropped, re-created and migrated; each test truncates the tables
first. Never point `TEST_PG*` at a database you care about (the `_test`
suffix guard exists to make that hard). Without `TEST_PGDATABASE`, database
tests are skipped and the pure unit tests still run.

## Commands

```bash
# Backend
cd backend
uv run pytest -q                 # all tests (db tests skipped without TEST_PG*)
uv run ruff check app tests migrations ../scripts
uv run ruff format --check app tests migrations ../scripts
uv run mypy app                  # strict

# Frontend
cd frontend
npm run typecheck && npm run lint && npm test && npm run build
npx playwright install chromium  # once
npm run test:e2e                 # starts the backend in demo mode on :8799

# Everything
./scripts/check.sh
```

The Playwright config migrates the test database (`bootstrap_db.py`), starts
`python -m app` in demo mode with `TEST_PG*` mapped to `PG*` and provider keys
blanked, and serves the production frontend build, so run `npm run build`
first (or use `scripts/check.sh`, which does).

## What is covered

| Area | Where |
| --- | --- |
| Hierarchy, tool permissions, exact graph edges, routing functions | `test_hierarchy_routing.py` |
| Review-loop limits, shared budgets, budget-bounded runs | `test_worker.py`, `test_redaction_budget.py` |
| Malformed structured output, refusal/truncation, corrective retry | `test_llm_and_retry.py`, `test_worker.py` |
| Provider error classification, Retry-After, deadline-aware backoff | `test_llm_and_retry.py` |
| URL blocking, IPv6/mapped/NAT64, DNS rebinding, redirects, size/type limits, decompression bombs | `test_url_safety.py` |
| Cross-run citation rejection (service + composite FKs), renumbering, excerpt verification | `test_sse_persistence.py`, `test_citations_markdown.py` |
| Markdown/XSS protection | `test_citations_markdown.py`, `frontend/src/test/markdown.test.tsx` |
| Secret redaction (logs, events, settings) | `test_redaction_budget.py` |
| Idempotent run creation, validation, queue cap, rate limit, CSRF/Origin/Host guard | `test_api.py` |
| Cancellation, interrupted-run recovery, atomic claims, single-worker lock | `test_worker.py` |
| Persisted SSE replay (Last-Event-ID, `?after=`), live wake-up, cleanup | `test_sse_persistence.py` |
| Database persistence and constraints | `test_sse_persistence.py` |
| Browser flow: question → live progress → cited report → Markdown download → reload → search; re-run | `frontend/e2e/demo-flow.spec.ts` |

## Not covered by automated tests

* Live calls to Anthropic and Tavily (would spend money). The adapters are
  tested with fakes that mirror the SDK/REST shapes, and the request
  parameters were checked against the installed SDK's signature.
* A live Aiven connection (no credentials during development).
