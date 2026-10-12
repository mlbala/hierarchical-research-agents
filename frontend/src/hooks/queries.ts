/** TanStack Query hooks: the single source of server state in the UI. */
import { keepPreviousData, useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { TERMINAL_STATUSES, type RunStatus } from '@/lib/types'

export const queryKeys = {
  config: ['config'] as const,
  projects: ['projects'] as const,
  project: (id: string) => ['project', id] as const,
  projectRuns: (id: string) => ['project', id, 'runs'] as const,
  recentRuns: ['runs', 'recent'] as const,
  run: (id: string) => ['run', id] as const,
  tasks: (id: string) => ['run', id, 'tasks'] as const,
  findings: (id: string) => ['run', id, 'findings'] as const,
  sources: (id: string) => ['run', id, 'sources'] as const,
  source: (runId: string, sourceId: string) => ['run', runId, 'source', sourceId] as const,
  report: (id: string) => ['run', id, 'report'] as const,
  search: (q: string) => ['search', q] as const,
}

const isActive = (status: RunStatus | undefined) => !!status && !TERMINAL_STATUSES.has(status)

export function useConfig() {
  return useQuery({ queryKey: queryKeys.config, queryFn: api.config, staleTime: 60_000 })
}

export function useProjects() {
  return useQuery({ queryKey: queryKeys.projects, queryFn: () => api.listProjects() })
}

export function useProject(projectId: string) {
  return useQuery({ queryKey: queryKeys.project(projectId), queryFn: () => api.getProject(projectId) })
}

export function useProjectRuns(projectId: string) {
  return useQuery({
    queryKey: queryKeys.projectRuns(projectId),
    queryFn: () => api.listProjectRuns(projectId, 50),
    // Keep history fresh while something in it is still running.
    refetchInterval: (q) => (q.state.data?.items.some((r) => isActive(r.status)) ? 4000 : false),
  })
}

export function useRecentRuns() {
  return useQuery({
    queryKey: queryKeys.recentRuns,
    queryFn: () => api.listRuns(8),
    refetchInterval: (q) => (q.state.data?.items.some((r) => isActive(r.status)) ? 5000 : 30_000),
  })
}

export function useRun(runId: string) {
  return useQuery({
    queryKey: queryKeys.run(runId),
    queryFn: () => api.getRun(runId),
    // SSE drives updates; this slow poll is only a safety net if the stream drops.
    refetchInterval: (q) => (isActive(q.state.data?.status) ? 10_000 : false),
  })
}

export function useTasks(runId: string, active: boolean) {
  return useQuery({
    queryKey: queryKeys.tasks(runId),
    queryFn: () => api.getTasks(runId),
    refetchInterval: active ? 10_000 : false,
  })
}

export function useFindings(runId: string) {
  return useQuery({ queryKey: queryKeys.findings(runId), queryFn: () => api.getFindings(runId) })
}

export function useSources(runId: string) {
  return useQuery({ queryKey: queryKeys.sources(runId), queryFn: () => api.getSources(runId) })
}

export function useSourceDetail(runId: string, sourceId: string | null) {
  return useQuery({
    queryKey: queryKeys.source(runId, sourceId ?? ''),
    queryFn: () => api.getSource(runId, sourceId!),
    enabled: !!sourceId,
  })
}

export function useReport(runId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.report(runId),
    queryFn: () => api.getReport(runId),
    enabled,
    retry: (count, error) => count < 2 && !(error instanceof Error && 'status' in error && error.status === 404),
  })
}

export function useReportSearch(q: string) {
  return useQuery({
    queryKey: queryKeys.search(q),
    queryFn: () => api.searchReports(q),
    enabled: q.trim().length >= 2,
    placeholderData: keepPreviousData,
  })
}
