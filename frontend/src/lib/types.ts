/** Types mirroring the backend's /api/v1 response models (app/schemas/api.py). */

export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type Depth = 'quick' | 'standard'
export type ExecutionMode = 'live' | 'demo'
export type ContentKind = 'fetched_page' | 'search_snippet' | 'demo'
export type SupportLevel = 'supported' | 'tentative' | 'conflicting'

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set([
  'completed',
  'partial',
  'failed',
  'cancelled',
  'interrupted',
])

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Project {
  id: string
  name: string
  description: string
  created_at: string
  updated_at: string
  run_count: number
  last_run_at: string | null
}

export interface RunSummary {
  id: string
  project_id: string
  question: string
  depth: Depth
  status: RunStatus
  execution_mode: ExecutionMode
  created_at: string
  started_at: string | null
  finished_at: string | null
  error_code: string | null
  is_partial: boolean | null
}

export interface RunWarning {
  code: string
  message: string
}

export interface RunDetail extends RunSummary {
  context: string | null
  input_urls: string[]
  plan: {
    objective?: string
    sub_questions?: string[]
    data_focus?: string
    context_focus?: string | null
  } | null
  warnings: RunWarning[]
  cancel_requested_at: string | null
  heartbeat_at: string | null
  input_tokens: number
  output_tokens: number
  llm_call_count: number
  tool_call_count: number
  /** Decimal string, or null when no pricing is configured (unknown, not zero). */
  estimated_cost_usd: string | null
  error_message: string | null
  settings_snapshot: {
    limits?: Record<string, number>
    llm_model?: string | null
    search_provider?: string
    max_parallel_agent_tasks?: number
  }
  report_id: string | null
  handoff: { summary?: string; confidence?: string; caveats?: string[] } | null
  source_count: number
  finding_count: number
}

export interface AgentTask {
  id: string
  parent_task_id: string | null
  agent_role: string
  task_key: string
  attempt: number
  status: 'running' | 'completed' | 'failed' | 'skipped' | 'cancelled' | 'interrupted'
  input_summary: string
  output: (Record<string, unknown> & { summary?: string }) | null
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  input_tokens: number
  output_tokens: number
  error_code: string | null
  error_message: string | null
}

export interface Finding {
  id: string
  task_id: string
  source_id: string
  agent_role: string
  claim: string
  evidence_excerpt: string
  support_level: SupportLevel
  review_status: 'pending' | 'approved' | 'rejected'
  created_at: string
}

export interface Source {
  id: string
  url: string
  title: string
  publisher: string | null
  published_at: string | null
  retrieved_at: string
  content_kind: ContentKind
  content_hash: string
  content_length: number
  excerpt: string
  truncated: boolean
}

export interface SourceDetail extends Source {
  content_text: string
}

export interface RunEvent {
  id: number
  task_id: string | null
  event_type: string
  level: 'debug' | 'info' | 'warning' | 'error'
  message: string
  payload: Record<string, unknown>
  created_at: string
}

export interface Citation {
  number: number
  source: Source
  evidence: { claim: string; excerpt: string; support_level: SupportLevel }[]
}

export interface Report {
  id: string
  run_id: string
  title: string
  executive_summary: string
  body_markdown: string
  limitations: string[]
  is_partial: boolean
  execution_mode: ExecutionMode
  created_at: string
  updated_at: string
  citations: Citation[]
}

export interface ReportSearchHit {
  report_id: string
  run_id: string
  project_id: string
  title: string
  question: string
  snippet: string
  is_partial: boolean
  execution_mode: ExecutionMode
  created_at: string
}

export interface ConfigIssue {
  key: string
  severity: 'error' | 'warning'
  message: string
}

export interface AppConfig {
  version: string
  execution_mode: ExecutionMode
  live_ready: boolean
  llm_provider: string
  llm_model: string | null
  search_provider: string
  issues: ConfigIssue[]
  limits: Record<Depth, Record<string, number>>
  max_parallel_agent_tasks: number
  max_concurrent_runs: number
  cost_estimates_enabled: boolean
}

export interface RunCreateInput {
  question: string
  context?: string | null
  input_urls: string[]
  depth: Depth
}
