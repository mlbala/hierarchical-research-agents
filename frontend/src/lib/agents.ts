/** Display metadata for the fixed seven-role hierarchy and workflow stages. */
import {
  BookOpenCheck,
  ClipboardList,
  Compass,
  FileText,
  LineChart,
  NotebookPen,
  Search,
  type LucideIcon,
} from 'lucide-react'

import type { AgentTask } from './types'

export type AgentRole =
  | 'research_director'
  | 'research_team_lead'
  | 'data_researcher'
  | 'market_researcher'
  | 'writing_team_lead'
  | 'technical_writer'
  | 'summary_writer'

export interface AgentInfo {
  role: AgentRole
  label: string
  short: string
  description: string
  icon: LucideIcon
}

export const AGENTS: Record<AgentRole, AgentInfo> = {
  research_director: {
    role: 'research_director',
    label: 'Research Director',
    short: 'Director',
    description: 'Plans the research, then receives the finished report and writes the handoff.',
    icon: Compass,
  },
  research_team_lead: {
    role: 'research_team_lead',
    label: 'Research Team Lead',
    short: 'Research lead',
    description: 'Assigns tasks, reviews evidence coverage, and may request one more pass.',
    icon: ClipboardList,
  },
  data_researcher: {
    role: 'data_researcher',
    label: 'Data Researcher',
    short: 'Data',
    description: 'Finds facts, statistics, dates and primary sources with exact excerpts.',
    icon: LineChart,
  },
  market_researcher: {
    role: 'market_researcher',
    label: 'Market Researcher',
    short: 'Market',
    description: 'Studies alternatives, trends and practical context, without inventing figures.',
    icon: Search,
  },
  writing_team_lead: {
    role: 'writing_team_lead',
    label: 'Writing Team Lead',
    short: 'Writing lead',
    description: 'Outlines the report, reviews the draft, and may request one revision.',
    icon: BookOpenCheck,
  },
  technical_writer: {
    role: 'technical_writer',
    label: 'Technical Writer',
    short: 'Writer',
    description: 'Writes the cited report, labelling inference, uncertainty and conflicts.',
    icon: FileText,
  },
  summary_writer: {
    role: 'summary_writer',
    label: 'Summary Writer',
    short: 'Summary',
    description: 'Summarises the finished report without adding new claims.',
    icon: NotebookPen,
  },
}

/** Display hierarchy: director -> two team leads -> two workers each. */
export const HIERARCHY: { lead: AgentRole; workers: AgentRole[] }[] = [
  { lead: 'research_team_lead', workers: ['data_researcher', 'market_researcher'] },
  { lead: 'writing_team_lead', workers: ['technical_writer', 'summary_writer'] },
]

export type AgentState = 'queued' | 'working' | 'done' | 'skipped' | 'failed' | 'stopped'

/** Current state of a role, derived from its most recent task. */
export function agentState(role: AgentRole, tasks: AgentTask[]): AgentState {
  const own = tasks.filter((t) => t.agent_role === role)
  if (own.some((t) => t.status === 'running')) return 'working'
  const latest = own.at(-1)
  if (!latest) return 'queued'
  switch (latest.status) {
    case 'completed':
      return 'done'
    case 'skipped':
      return 'skipped'
    case 'failed':
      return 'failed'
    default:
      return 'stopped'
  }
}

export interface StageInfo {
  key: string
  label: string
}

/** Ordered workflow stages, matching the `stage_changed` events from the server. */
export const STAGES: StageInfo[] = [
  { key: 'validating', label: 'Validate' },
  { key: 'planning', label: 'Plan' },
  { key: 'researching', label: 'Research' },
  { key: 'reviewing_evidence', label: 'Review evidence' },
  { key: 'outlining', label: 'Outline' },
  { key: 'drafting', label: 'Draft' },
  { key: 'reviewing_draft', label: 'Review draft' },
  { key: 'summarizing', label: 'Summarise' },
  { key: 'validating_citations', label: 'Check citations' },
  { key: 'handoff', label: 'Handoff' },
  { key: 'finalizing', label: 'Finalise' },
]

/** Who each role reports to (mirrors ROLE_PARENT in the backend). */
export const ROLE_PARENT_LABEL: Record<AgentRole, string | null> = {
  research_director: null,
  research_team_lead: 'Research Director',
  writing_team_lead: 'Research Director',
  data_researcher: 'Research Team Lead',
  market_researcher: 'Research Team Lead',
  technical_writer: 'Writing Team Lead',
  summary_writer: 'Writing Team Lead',
}
