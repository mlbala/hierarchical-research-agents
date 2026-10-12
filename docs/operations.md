# Operations

## Running

```bash
cd frontend && npm run build          # once per frontend change
cd backend  && uv run python -m app   # API + UI at http://127.0.0.1:8000
```

Always a single process (`workers=1`). The research worker starts inside it
and holds a PostgreSQL advisory lock; an accidental second process stays in
standby (`/api/v1/health/ready` → `"worker": "standby"`).

## Health

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health/live` | process is up (no dependencies) |
| `GET /api/v1/health/ready` | database reachable, migrations at head, configuration problems (keys only, never values), worker `active`/`standby`; `503` when not ready |
| `GET /api/v1/config` | sanitised configuration readiness for the UI |

Neither endpoint calls a paid provider.

## Bounded execution (defaults)

| Limit | Standard | Quick |
| --- | --- | --- |
| Model calls per run (incl. retries) | 20 | 12 |
| Search calls per run (incl. retries) | 12 | 4 |
| Page fetches per run | 24 | 8 |
| Stored sources per run | 20 | 10 |
| Extra research passes | 1 | 0 |
| Writing revisions | 1 | 0 |
| Run deadline | 600 s | 300 s |
| Queries / pages per researcher per pass | 3 / 5 | 2 / 3 |

Per operation: 60 s LLM timeout, 15 s page timeout, 2 MB download cap, 30,000
extracted characters per source, 2 transient retries with exponential backoff
and jitter (provider `Retry-After` honoured, never past the run deadline).
Globally: 1 active run, 2 parallel agent tasks, 10 queued runs, 6 run
creations per client per minute. All are configurable in `.env`.

Budgets are reserved before each call under a lock shared by parallel tasks,
and retries reserve again. Optional loops are taken only when enough model
calls remain for the writing phase, so budget exhaustion produces a partial
report rather than an aborted one whenever possible.

With large models, a full report draft can approach the 60 s LLM timeout. If
drafts time out, raise `LLM_TIMEOUT_SECONDS`, choose a faster model or set a
lower `ANTHROPIC_EFFORT`.

## Restarts, cancellation and failures

* **Queued runs** stay queued across restarts and start automatically.
* **Running runs** at the time of a crash are marked `interrupted` once the
  new process acquires the worker lock (or `cancelled` if cancellation had
  been requested). Graceful shutdown marks them `interrupted` directly. Runs
  are never resumed; use **Re-run** to start a new run with the same inputs.
  External calls are not exactly-once: a crash can repeat a search or model
  call in the re-run.
* **Cancellation** is persisted, checked before every task and tool call, and
  in-flight model/HTTP calls are cancelled. Evidence gathered so far is kept.
* **Failures** end with `failed`, a stable `error_code` and a safe message
  (e.g. `NO_USABLE_EVIDENCE`, `LLM_AUTH_FAILED`, `RUN_TIMEOUT`,
  `BUDGET_EXCEEDED`). Unexpected exceptions are logged once with context.
* Closing the browser never cancels a run; reopening it replays progress from
  the database.

## Logging

JSON lines on stdout via structlog: UTC `timestamp`, `level`, `event`, and,
where relevant, `request_id`, `run_id`, `task_id`, `agent_role`,
`duration_ms`, `retry_attempt`, `provider`, `model`, token counts and
`error_code`. Logged: request summaries (path without query string),
run/task lifecycle, provider and tool failures, retries, cancellations,
budget limits and security rejections. Not logged: prompts, responses,
fetched documents, database URLs, passwords, API keys, authorization headers
or URL query strings. User-visible progress lives separately in `run_events`.

## Costs

The open-source stack is free to run, but **the services it calls are not**:

* OpenAI (default) or Anthropic API usage is billed per token. With
  `gpt-5-nano` ($0.05 / $0.40 per million input / output tokens, checked
  2026-10-12; retired by OpenAI on 2026-12-11, then use `gpt-6-luna` at
  $0.10 / $0.50) a Standard run costs roughly one to two cents. Reasoning
  tokens count as output, so a low reasoning effort keeps costs down.
  When prepaid credit runs out the run fails with `LLM_QUOTA_EXCEEDED`.
* Tavily search is billed per credit beyond the free 1,000 credits a month (a
  Standard run uses at most 12 basic searches).
* Managed PostgreSQL (e.g. Aiven) and any hosting are billed by their providers.

Token usage from provider responses is stored per task and per run. An
estimated LLM cost is shown only when you set both
`LLM_INPUT_USD_PER_MTOK` and `LLM_OUTPUT_USD_PER_MTOK`; otherwise the UI shows
"unknown", never zero. Search and hosting costs are not estimated. Demo mode
makes no paid calls.
