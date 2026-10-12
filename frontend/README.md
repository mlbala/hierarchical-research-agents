# Research Hive — frontend

React + TypeScript + Vite UI for Research Hive, styled with Tailwind CSS and
shadcn/ui (Radix), icons from Lucide, and server state via TanStack Query.

See the repository [README](../README.md) for setup. Common commands:

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api to the backend on :8000
npm run typecheck  # tsc -b
npm run lint       # oxlint
npm test           # vitest unit tests
npm run build      # production build served by FastAPI from frontend/dist
npm run test:e2e   # Playwright demo flow (needs TEST_PG* — see docs/testing.md)
```

The browser never sees secrets: there are no `VITE_*` secret variables, and all
provider keys stay on the server.
