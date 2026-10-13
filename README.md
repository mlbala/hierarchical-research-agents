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

Saved reports also offer **Download PDF**: a paginated A4 document with the
summary, findings, analysis, limitations and clickable source references.
PDF export uses ReportLab and the saved report content, retains demo/partial
notices, and makes no additional model or web calls. Markdown download and
copy remain available. After updating the project, run `uv sync` in `backend/`
and rebuild the frontend with `npm run build` to enable the new option.

> **Costs.** The application is open source, but the services it uses are
> not free: OpenAI (or Anthropic) API calls are billed per token, Tavily
> searches are billed per request beyond the free monthly credits, and managed
> PostgreSQL (e.g. Aiven) or hosting is billed by its provider. Demo mode makes
> no paid calls.

> **Access.** There is no login. Everyone who can reach the server shares one
> workspace. It binds to `127.0.0.1` by default; read
> [docs/security.md](docs/security.md) before exposing it on a network.

## Technology stack

| Layer | Technologies |
| --- | --- |
| **Frontend** | React 19 and TypeScript, built with Vite. React Router for pages, TanStack Query for server data, Tailwind CSS 4 with shadcn/ui (Radix) components, React Markdown for reports, Server-Sent Events (SSE) for live progress. |
| **Backend API** | Python 3.12+, FastAPI and Uvicorn. One async server provides the API and serves the built frontend. Pydantic validates configuration, requests and agent outputs; structlog writes redacted JSON logs. |
| **Agents** | LangGraph runs the explicit seven-role workflow. The OpenAI SDK (default model `gpt-6-luna`) or the Anthropic SDK produces structured outputs. |
| **Web research** | Tavily for search. An SSRF-safe page fetcher (httpcore with the certifi CA bundle) reads HTML, plain-text and Markdown pages; BeautifulSoup and lxml extract readable text. |
| **Reports** | Markdown, shown in the UI and downloadable; paginated PDF export with ReportLab. |
| **Persistence** | PostgreSQL 14+ (e.g. Aiven) through SQLAlchemy 2.1 and Psycopg 3, with Alembic migrations. The database also provides the durable run queue and worker lock, so Redis and Celery are not needed. |
| **Packaging and deployment** | Multi-stage Dockerfile (a Node 22 stage builds the UI; a slim Python 3.12 image runs it as a non-root user) and Docker Compose. uv manages Python dependencies, with exported pip requirements files; npm manages the frontend. |
| **Quality checks** | Ruff, mypy and pytest for the backend; TypeScript, oxlint, Vitest and Playwright for the frontend. |

## From question to report

1. Create a project, enter a question and optional context/reference URLs,
   and choose Quick or Standard depth.
2. The API saves a queued run in PostgreSQL. The background worker claims
   it and starts the LangGraph workflow.
3. The director plans the research. The research lead assigns complementary
   tasks to the data and market researchers, which work in parallel.
4. Researchers collect sources and verified excerpts. Their lead reviews
   the findings and can request a bounded extra research pass.
5. The writing lead outlines the report from approved evidence. The technical
   writer drafts it, the lead reviews it, and the summary writer summarizes
   the resulting report.
6. Code validates and renumbers citations; the director adds a handoff note.
   The backend saves the report and its source links. The UI receives progress
   events throughout and lets you inspect evidence or download Markdown.

See [Architecture](docs/architecture.md) for the exact graph, backend layers
and how the agents exchange structured results.

## Running the app

The app runs in one of two modes, chosen with `DEMO_MODE` in `.env`:

* **Demo mode** (`DEMO_MODE=true`): offline, fictional fixtures, no API keys
  and no cost. Good for trying the UI.
* **Live mode** (`DEMO_MODE=false`): real web research with OpenAI (or
  Anthropic) and Tavily. Each run costs a little; see [Live mode](#live-mode).

Both modes need a PostgreSQL database. You can run the app **with Docker**
(recommended: nothing else to install) or **from source**.

### Requirements

* **Docker route:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  (or Docker Engine with Compose v2). Python and Node.js are not needed.
* **From-source route:** Python 3.12+ with [uv](https://docs.astral.sh/uv/),
  and Node.js 20.19+ (22 LTS recommended) with npm.
* **Both:** a PostgreSQL 14+ database (Aiven or local) in which the app may
  create a schema.
* **Live mode only:** an [OpenAI API key](https://platform.openai.com/api-keys)
  (or an Anthropic key) and a [Tavily API key](https://app.tavily.com/).

### 1. Configure

```bash
git clone git@github.com:mlbala/hierarchical-research-agents.git
cd hierarchical-research-agents
cp .env.example .env
```

Edit `.env`: set the `PG*` connection values, choose `DEMO_MODE`, and add the
provider keys for live mode. For Aiven, save the CA certificate as
`certs/aiven-ca.pem` and keep `PGSSLROOTCERT=./certs/aiven-ca.pem` (see
[Aiven setup](docs/aiven.md)). `.env` and `certs/` are git-ignored.

### 2a. Run with Docker (recommended)

From the repository root:

```bash
docker compose build                                          # build the image
docker compose run --rm app python ../scripts/bootstrap_db.py # create schema + migrate
docker compose up -d                                          # start in the background
```

Open **http://127.0.0.1:8000**. Create a project, ask a question, choose
Quick or Standard depth and watch the agents work. Demo reports are about
fictional e-bike subsidy programmes and are labelled as such.

Everyday commands:

| Task | Command |
| --- | --- |
| Start | `docker compose up -d` |
| Follow the logs | `docker compose logs -f app` |
| Check status and health | `docker compose ps` |
| Rebuild after code changes | `docker compose up -d --build` |
| Stop | `docker compose down` (data stays in your database) |
| Check the database without changes | `docker compose run --rm app python ../scripts/bootstrap_db.py --check` |

Run the bootstrap step before the first start and after migration changes. It
is safe to rerun against an initialized database; no migrations run
automatically on server start. The container restarts on its own when Docker
starts (`restart: unless-stopped`) unless you stopped it with
`docker compose down`.

How the container is set up:

* The root `Dockerfile` builds the React frontend in a Node stage, then copies
  the build into a non-root Python runtime image (UID 10001). See
  [Docker's multi-stage build guide](https://docs.docker.com/build/building/multi-stage/)
  for background on this layout.
* Compose injects `.env` at runtime and mounts `certs/` read-only; neither is
  baked into the image. Use relative paths such as
  `PGSSLROOTCERT=./certs/aiven-ca.pem`; absolute host paths do not exist
  inside the container. The certificate must be readable by UID 10001 (a CA
  certificate is public).
* Compose sets `HOST=0.0.0.0` inside the container and publishes port 8000
  only on the host's loopback interface. The UI and API share that one
  address, so no CORS setup is needed.
* There is no database service in Compose; the app uses your existing
  PostgreSQL. The database host must be reachable from Docker: `localhost`
  means the container itself, so for PostgreSQL on the Docker Desktop host use
  `host.docker.internal` with suitable TLS settings.
* The healthcheck tests process liveness; `/api/v1/health/ready` also checks
  the database, migrations, provider configuration and worker. Keep one app
  instance; a PostgreSQL advisory lock governs execution.

To use the Dockerfile without Compose:

```bash
docker build -t research-hive:local .
docker run --rm --env-file .env -v "$(pwd)/certs:/app/certs:ro" research-hive:local python ../scripts/bootstrap_db.py
docker run --rm --init --env-file .env -e HOST=0.0.0.0 -e PORT=8000 -p 127.0.0.1:8000:8000 -v "$(pwd)/certs:/app/certs:ro" research-hive:local
```

### 2b. Run from source

```bash
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

To install the backend with pip instead of uv, see [Installing with pip](#installing-with-pip).

### Development with hot reload

Run the backend and the Vite dev server in separate terminals, after
installing dependencies and applying the database migrations. Vite proxies
`/api` (including SSE) to the backend:

```bash
cd backend  && uv run python -m app --reload   # http://127.0.0.1:8000
cd frontend && npm run dev                      # http://localhost:5173
```

Check readiness with:

```bash
curl http://127.0.0.1:8000/api/v1/health/ready
```

Readiness checks database access, migrations, provider configuration and worker
state without making paid provider calls.

### Database setup and connection checks

Demo mode still requires PostgreSQL: it replaces LLM, search and page-fetch
calls with fixtures, while persistence and the worker run normally.

Set `PGDATABASE` to the exact name of an existing database, and `DB_SCHEMA`
to the namespace for this application's tables (default `research_hive`).
The database and schema are different objects: successfully connecting or
running `SELECT VERSION()` does not create the application tables.

```bash
# From the repository root, after uv sync in backend/
backend/.venv/bin/python scripts/bootstrap_db.py --check  # read-only status
backend/.venv/bin/python scripts/bootstrap_db.py          # create schema + migrate
```

`--check` exits with status 1 when migrations are missing or behind; it does
not initialize them. If it reports `Connected` followed by `Schema ... missing`,
connectivity works: run the command without `--check`. The bootstrap script
creates the configured schema and applies migrations, but never creates the
database or database roles. Its user must have permission to create the schema.

This project reads `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`,
`PGSSLMODE` and `PGSSLROOTCERT`. Older applications' `DB_HOST`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD` and `DATABASE_URL` settings are not supported aliases.
Relative CA paths are resolved from the repository root. Restart the backend
after changing `.env`. See [Aiven setup](docs/aiven.md) for remote TLS setup.

## Live mode

OpenAI is the default provider. Set in `.env`:

```ini
DEMO_MODE=false
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-6-luna            # cheapest current-generation OpenAI model
OPENAI_REASONING_EFFORT=low        # none|low|medium|high|xhigh|max; lower = cheaper
TAVILY_API_KEY=tvly-...
LLM_INPUT_USD_PER_MTOK=0.10        # gpt-6-luna prices, for the cost estimate
LLM_OUTPUT_USD_PER_MTOK=0.50
```

A Standard run uses roughly 65k input and 15–40k output tokens. Prices from
OpenAI's pricing page (checked 2026-10-12), per million input / output tokens:

| Model | Price | ≈ cost per Standard run | Notes |
| --- | --- | --- | --- |
| `gpt-6-luna` (default) | $0.10 / $0.50 | ~$0.01–0.03 | latest generation, built for high-volume focused tasks |
| `gpt-5-nano` | $0.05 / $0.40 | ~$0.01–0.02 | slightly cheaper, older; OpenAI retires it on 2026-12-11 |
| `gpt-5.6-luna` | $0.20 / $1.20 | ~$0.02–0.06 | older and pricier than `gpt-6-luna` |
| `gpt-5-mini` | $0.25 / $2.00 | ~$0.05–0.10 | not cheaper |
| `gpt-6.1-sol` | $2 / $10 | ~$0.30–0.55 | best writing, highest cost |

When you change the model, update the two price settings too.

Anthropic remains supported: set `LLM_PROVIDER=anthropic`,
`ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (e.g. `claude-haiku-5-5` for low cost
or `claude-opus-5-5` for the highest quality); `ANTHROPIC_EFFORT` and
`ANTHROPIC_REFUSAL_FALLBACK` are optional.

Live mode fails clearly (UI warning, `503 CONFIGURATION_ERROR` on run
creation, readiness check) when a key or the model is missing, and a run stops
with `LLM_QUOTA_EXCEEDED` when the provider account runs out of credit.
Without both price settings the estimated cost is shown as "unknown".

## Testing

### Quality checks

```bash
./scripts/check.sh      # ruff, mypy, pytest, tsc, oxlint, vitest, build, Playwright
```

Database tests and the Playwright suite need a dedicated test database,
configured with `TEST_PG*` variables (its name must end in `_test`). Without
one they are skipped. Details are in [docs/testing.md](docs/testing.md).

### Testing the UI

**By hand.** With the app running, open http://127.0.0.1:8000 and check that:

1. the home page loads and recent research appears in the sidebar;
2. a new project can be created and a question started (Quick depth);
3. the run page shows live agent progress and finishes as Completed or Partial;
4. the Report and Sources tabs work, and a citation marker such as `[1]`
   opens its source;
5. **Download .md** and **Download PDF** save files;
6. **Search reports** finds the saved report.

In live mode, step 2 makes paid API calls (a Quick run costs about $0.005 with
`gpt-6-luna`) and saves the run to your database.

**Automated.** The Playwright suite drives a real browser through the full
flow (create a project, run research, read the cited report, download it,
search, re-run). It starts its own backend in demo mode on port 8799, so it
makes no paid calls and never touches your real data. A throwaway PostgreSQL
container provides the test database:

```bash
# 1. Throwaway test database (deleted when stopped)
docker run -d --rm --name hive-e2e-db -e POSTGRES_PASSWORD=e2e \
  -e POSTGRES_DB=research_hive_test -p 127.0.0.1:55433:5432 postgres:16-alpine

# 2. Build the UI and run the browser tests
cd frontend
npm ci && npx playwright install chromium   # first time only
npm run build
TEST_PGHOST=127.0.0.1 TEST_PGPORT=55433 TEST_PGDATABASE=research_hive_test \
TEST_PGUSER=postgres TEST_PGPASSWORD=e2e TEST_PGSSLMODE=disable \
npm run test:e2e                  # add  -- --headed  to watch, or  -- --ui  for the interactive runner

# 3. Clean up
docker stop hive-e2e-db
```

The suite runs the backend from source rather than from the Docker image, so
it tests the same UI and API code, not the image itself.

## Development

### Installing with pip

Python 3.12+ users can install the locked backend dependencies without uv:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r ../requirements.txt
# For lint, type checks and tests, install requirements-dev.txt instead:
# python -m pip install -r ../requirements-dev.txt
python ../scripts/bootstrap_db.py
python -m app
```

Both files live at the repository root and include pinned transitive dependencies without hashes;
`requirements-dev.txt` includes runtime dependencies plus development tools.
`pyproject.toml` and `uv.lock` remain the source of truth. Regenerate the files
after dependency changes, from `backend/`:

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes --output-file ../requirements.txt
uv export --frozen --no-emit-project --no-hashes --output-file ../requirements-dev.txt
```

Frontend dependencies still use `npm ci` in `frontend/`.

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
| `LLM_PROVIDER` | `openai` | `openai` or `anthropic` |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` | —, `gpt-6-luna` in template, `low` | OpenAI live provider |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | — | Anthropic live provider |
| `TAVILY_API_KEY` | — | live web search |
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
