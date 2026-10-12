import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end tests run the real backend in DEMO mode (no external or paid
 * calls) serving the production frontend build, against a dedicated test
 * database given by TEST_PG* variables. The database name must end in
 * "_test" so these tests can never touch real research records.
 */
const port = Number(process.env.E2E_PORT ?? 8799)
const database = process.env.TEST_PGDATABASE ?? ''
if (!database.endsWith('_test')) {
  throw new Error('Set TEST_PGDATABASE (ending in "_test") and TEST_PG* to run e2e tests.')
}

const serverEnv: Record<string, string> = {
  PGHOST: process.env.TEST_PGHOST ?? '127.0.0.1',
  PGPORT: process.env.TEST_PGPORT ?? '5432',
  PGDATABASE: database,
  PGUSER: process.env.TEST_PGUSER ?? 'postgres',
  PGPASSWORD: process.env.TEST_PGPASSWORD ?? '',
  PGSSLMODE: process.env.TEST_PGSSLMODE ?? 'disable',
  PGSSLROOTCERT: process.env.TEST_PGSSLROOTCERT ?? '',
  DEMO_MODE: 'true',
  DEMO_STEP_DELAY_MS: '150',
  HOST: '127.0.0.1',
  PORT: String(port),
  ALLOWED_HOSTS: '127.0.0.1,localhost',
  APP_ORIGIN: `http://127.0.0.1:${port}`,
  LOG_LEVEL: 'WARNING',
  // Never inherit live provider keys from the shell during tests.
  ANTHROPIC_API_KEY: '',
  TAVILY_API_KEY: '',
}

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // Apply migrations to the test database, then serve API + built UI.
    command:
      'uv run --project ../backend python ../scripts/bootstrap_db.py && ' +
      'cd ../backend && uv run python -m app',
    url: `http://127.0.0.1:${port}/api/v1/health/ready`,
    env: serverEnv,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
