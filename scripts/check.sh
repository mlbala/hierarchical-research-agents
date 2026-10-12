#!/usr/bin/env bash
# Run every quality gate: lint, formatting, type checks, unit/integration tests
# and the production frontend build. Database-backed tests and the Playwright
# suite run only when TEST_PGDATABASE is set (see docs/testing.md).
#
#   ./scripts/check.sh            # everything available
#   E2E=0 ./scripts/check.sh      # skip the browser tests
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Backend: ruff, format, mypy"
cd "$ROOT/backend"
uv run ruff check app tests migrations ../scripts
uv run ruff format --check app tests migrations ../scripts
uv run mypy app

echo "==> Backend: pytest"
if [[ -z "${TEST_PGDATABASE:-}" ]]; then
  echo "    TEST_PGDATABASE not set: database tests will be skipped."
fi
uv run pytest -q -p no:logging

echo "==> Frontend: typecheck, lint, unit tests, build"
cd "$ROOT/frontend"
npm run --silent typecheck
npm run --silent lint
npm run --silent test
npm run --silent build

if [[ -n "${TEST_PGDATABASE:-}" && "${E2E:-1}" != "0" ]]; then
  echo "==> Frontend: Playwright end-to-end (demo mode)"
  npm run --silent test:e2e
else
  echo "==> Skipping Playwright (set TEST_PGDATABASE, or E2E=0 to silence)."
fi

echo "All checks passed."
