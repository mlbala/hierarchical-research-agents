import { createBrowserRouter } from 'react-router'

import { AppShell } from '@/components/layout/AppShell'
import { HomePage } from '@/pages/HomePage'

/**
 * Routes. Heavier pages are loaded lazily so the first paint stays small;
 * the run page is keyed by run id so per-run state starts fresh.
 */
export const router = createBrowserRouter([
  {
    Component: AppShell,
    children: [
      { index: true, Component: HomePage },
      {
        path: 'projects/:projectId',
        lazy: async () => ({ Component: (await import('@/pages/ProjectPage')).ProjectPage }),
      },
      {
        path: 'runs/:runId',
        lazy: async () => ({ Component: (await import('@/pages/RunPage')).KeyedRunPage }),
      },
      {
        path: 'search',
        lazy: async () => ({ Component: (await import('@/pages/SearchPage')).SearchPage }),
      },
      {
        path: '*',
        lazy: async () => ({ Component: (await import('@/pages/NotFoundPage')).NotFoundPage }),
      },
    ],
  },
])
