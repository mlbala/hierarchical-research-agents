# Research Hive

A hierarchical multi-agent research platform. Ask a question; a small team of
AI agents plans the research, gathers real sources from the web, reviews the
evidence and writes a readable report in which every key claim cites a
numbered source. Runs, evidence and reports are saved in PostgreSQL.

![Run view with the agent hierarchy and a cited report](docs/images/run-report.png)

* **Seven fixed roles**: Research Director → Research Team Lead (Data and
  Market Researchers) and Writing Team Lead (Technical and Summary Writers),
  orchestrated by an explicit LangGraph workflow with bounded loops.
* **Evidence you can check**: verbatim excerpts verified against the stored
  source text, snippet-only sources labelled as such, citations validated and
  renumbered in code, partial reports clearly marked.
* **Guardrails in code**: SSRF-safe page retrieval, role-based tool
  permissions, shared budgets and deadlines, cancellation, and a single
  durable worker.
* **Calm UI**: live progress over SSE, an HTML/CSS agent hierarchy, sources and
  evidence tabs, citation previews, Markdown copy/download, saved-report
  search, light and dark themes.
* **Demo mode**: runs completely offline on clearly fictional fixtures, with no
  API keys and no external calls.

> **Costs.** The application is open source, but the services it uses are
> not free: Anthropic API calls are billed per token, Tavily searches are
> billed per request, and managed PostgreSQL (e.g. Aiven) or hosting is billed
> by its provider. Demo mode makes no paid calls.

> **Access.** There is no login. Everyone who can reach the server shares one
> workspace. It binds to `127.0.0.1` by default; read
> [docs/security.md](docs/security.md) before exposing it on a network.

## Requirements

* Python 3.12+ and [uv](https://docs.astral.sh/uv/)
* Node.js 20.19+ (22 LTS recommended) and npm
* PostgreSQL 14+ (Aiven or local) — the app needs one database it can create a
  schema in
* Live mode only: an [Anthropic API key](https://console.anthropic.com/) and a
  [Tavily API key](https://app.tavily.com/)

## Quick start (demo mode)

```bash
git clone git@github.com:mlbala/hierarchical-research-agents.git
cd hierarchical-research-agents
cp .env.example .env            # DEMO_MODE=true by default; set the PG* values

# Backend dependencies + database schema
cd backend
uv sync
uv run python ../scripts/bootstrap_db.py     # checks TLS/connection, creates schema, migrates

# Frontend build (served by FastAPI)
cd ../frontend
npm ci
npm run build

# Run
cd ../backend
uv run python -m app            # open http://127.0.0.1:8000
```

Create a project, ask a question (the demo fixtures are about e-bike subsidy
programmes) and watch the agents work. Demo reports are labelled as fictional.

## Live mode

Set in `.env`:

```ini
DEMO_MODE=false
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-5-5    # verified current model ID; claude-sonnet-5-5 is cheaper
TAVILY_API_KEY=tvly-...
```

The model is configurable. Live mode fails clearly (UI warning, `503
CONFIGURATION_ERROR` on run creation, readiness check) when a key or the model
is missing. Optional: `ANTHROPIC_EFFORT` (`low`…`max`), and
`LLM_INPUT_USD_PER_MTOK` / `LLM_OUTPUT_USD_PER_MTOK` to show estimated LLM cost
(otherwise shown as "unknown"). For supported models the adapter opts into
Anthropic's server-side refusal fallback (`ANTHROPIC_REFUSAL_FALLBACK=false`
to disable).

## Development

Run the backend and the Vite dev server side by side; Vite proxies `/api`
(including SSE) to the backend:

```bash
cd backend  && uv run python -m app --reload   # http://127.0.0.1:8000
cd frontend && npm run dev                      # http://localhost:5173
```

Checks and tests (details in [docs/testing.md](docs/testing.md)):

```bash
./scripts/check.sh      # ruff, mypy, pytest, tsc, oxlint, vitest, build, Playwright
```

Database tests and the Playwright suite need a dedicated test database
configured via `TEST_PG*` variables (name must end in `_test`).

## Configuration

All settings are environment variables (see [.env.example](.env.example)).
The essentials:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | bind address |
| `APP_ORIGIN`, `ALLOWED_HOSTS` | Vite origin, `localhost,127.0.0.1` | browser origin and Host allowlists |
| `DEMO_MODE` | `true` | offline fixtures vs. live providers |
| `PGHOST` … `PGSSLROOTCERT` | — | PostgreSQL connection (structured parameters, `verify-full` TLS) |
| `DB_SCHEMA` | `research_hive` | all tables live in this schema |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `3`, `2` | small pool for a small managed database |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TAVILY_API_KEY` | — | live providers |
| `MAX_*`, `RUN_TIMEOUT_SECONDS` | see [operations](docs/operations.md) | per-run budgets and limits |

## Documentation

* [Architecture](docs/architecture.md) — system diagram, agent responsibilities, workflow, evidence pipeline
* [Database](docs/database.md) — schema, tables, constraints, migrations, least-privilege roles, pgvector notes
* [Aiven setup](docs/aiven.md) — connection details, CA certificate, database creation
* [Security](docs/security.md) — threat model, guardrails, private-network deployment, limitations
* [Operations](docs/operations.md) — running, health checks, limits, restarts, logging, costs
* [Testing](docs/testing.md) — test database setup and coverage

## Project layout

```
backend/    FastAPI app (app/), Alembic migrations, pytest suite
frontend/   React + TypeScript + Vite + Tailwind + shadcn/ui, Vitest, Playwright
scripts/    bootstrap_db.py (schema + migrations), check.sh (all quality gates)
docs/       architecture, database, Aiven, security, operations, testing
```

## License

[MIT](LICENSE)
