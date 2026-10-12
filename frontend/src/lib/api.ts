/**
 * Thin client for the Research Hive REST API.
 *
 * Every state-changing request sends JSON plus the `X-Research-Hive` header
 * that the backend requires as a cross-site request guard. Errors are
 * normalised into `ApiError` using the server's error envelope; nothing here
 * ever handles secrets (they live only on the server).
 */
import type {
  AgentTask,
  AppConfig,
  Finding,
  Page,
  Project,
  Report,
  ReportSearchHit,
  RunCreateInput,
  RunDetail,
  RunEvent,
  RunSummary,
  Source,
  SourceDetail,
} from './types'

export const API_BASE = '/api/v1'

export interface ApiErrorDetail {
  field: string
  message: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly retryable: boolean
  readonly details: ApiErrorDetail[]

  constructor(options: {
    status: number
    code: string
    message: string
    requestId?: string | null
    retryable?: boolean
    details?: ApiErrorDetail[]
  }) {
    super(options.message)
    this.name = 'ApiError'
    this.status = options.status
    this.code = options.code
    this.requestId = options.requestId ?? null
    this.retryable = options.retryable ?? false
    this.details = options.details ?? []
  }
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as {
      error?: {
        code?: string
        message?: string
        request_id?: string
        retryable?: boolean
        details?: ApiErrorDetail[]
      }
    }
    if (body.error) {
      return new ApiError({
        status: response.status,
        code: body.error.code ?? 'HTTP_ERROR',
        message: body.error.message ?? 'The request failed.',
        requestId: body.error.request_id,
        retryable: body.error.retryable,
        details: body.error.details,
      })
    }
  } catch {
    // Not JSON (for example a proxy error page); fall through.
  }
  return new ApiError({
    status: response.status,
    code: 'HTTP_ERROR',
    message: `The server responded with status ${response.status}.`,
    retryable: response.status >= 500,
  })
}

async function request<T>(
  path: string,
  init: { method?: 'GET' | 'POST'; body?: unknown; headers?: Record<string, string> } = {},
): Promise<T> {
  const method = init.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json', ...init.headers }
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json'
    headers['X-Research-Hive'] = '1'
  }
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: method === 'GET' ? undefined : JSON.stringify(init.body ?? {}),
      credentials: 'same-origin',
    })
  } catch {
    throw new ApiError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: 'Cannot reach the Research Hive server. Check that it is running.',
      retryable: true,
    })
  }
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as T
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  config: () => request<AppConfig>('/config'),

  listProjects: (limit = 50, offset = 0) =>
    request<Page<Project>>(`/projects${query({ limit, offset })}`),
  getProject: (projectId: string) => request<Project>(`/projects/${projectId}`),
  createProject: (input: { name: string; description: string }) =>
    request<Project>('/projects', { method: 'POST', body: input }),
  listProjectRuns: (projectId: string, limit = 20, offset = 0) =>
    request<Page<RunSummary>>(`/projects/${projectId}/runs${query({ limit, offset })}`),
  createRun: (projectId: string, input: RunCreateInput, idempotencyKey: string) =>
    request<RunSummary>(`/projects/${projectId}/runs`, {
      method: 'POST',
      body: input,
      headers: { 'Idempotency-Key': idempotencyKey },
    }),

  listRuns: (limit = 10, offset = 0) =>
    request<Page<RunSummary>>(`/runs${query({ limit, offset })}`),
  getRun: (runId: string) => request<RunDetail>(`/runs/${runId}`),
  cancelRun: (runId: string) => request<RunSummary>(`/runs/${runId}/cancel`, { method: 'POST' }),
  rerun: (runId: string, idempotencyKey: string) =>
    request<RunSummary>(`/runs/${runId}/rerun`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  getTasks: (runId: string) => request<AgentTask[]>(`/runs/${runId}/tasks`),
  getFindings: (runId: string, limit = 100, offset = 0) =>
    request<Page<Finding>>(`/runs/${runId}/findings${query({ limit, offset })}`),
  getSources: (runId: string, limit = 100, offset = 0) =>
    request<Page<Source>>(`/runs/${runId}/sources${query({ limit, offset })}`),
  getSource: (runId: string, sourceId: string) =>
    request<SourceDetail>(`/runs/${runId}/sources/${sourceId}`),
  getEvents: (runId: string, after = 0) =>
    request<RunEvent[]>(`/runs/${runId}/events${query({ after, limit: 500 })}`),
  getReport: (runId: string) => request<Report>(`/runs/${runId}/report`),
  reportMarkdownUrl: (runId: string) => `${API_BASE}/runs/${runId}/report.md`,
  async downloadReportPdf(runId: string): Promise<void> {
    const response = await fetch(`${API_BASE}/runs/${runId}/report.pdf`)
    if (!response.ok) throw await parseError(response)
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]
      ?? 'research-report.pdf'
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  },
  async getReportMarkdown(runId: string): Promise<string> {
    const response = await fetch(`${API_BASE}/runs/${runId}/report.md`)
    if (!response.ok) throw await parseError(response)
    return response.text()
  },

  searchReports: (q: string, limit = 20, offset = 0) =>
    request<Page<ReportSearchHit>>(`/search/reports${query({ q, limit, offset })}`),
}

/** A fresh idempotency key; reuse it when retrying the *same* submission. */
export function newIdempotencyKey(): string {
  return `web-${crypto.randomUUID()}`
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return 'Something went wrong. Please try again.'
}
