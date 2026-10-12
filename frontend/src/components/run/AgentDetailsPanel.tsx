import { Clock, Coins } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { AGENTS, ROLE_PARENT_LABEL, type AgentRole } from '@/lib/agents'
import { formatDuration, formatNumber } from '@/lib/format'
import type { AgentTask } from '@/lib/types'

const STATUS_CLASS: Record<AgentTask['status'], string> = {
  running: 'text-primary',
  completed: 'text-success',
  failed: 'text-destructive',
  skipped: 'text-muted-foreground',
  cancelled: 'text-muted-foreground',
  interrupted: 'text-warning',
}

/** Short action summaries and results for one agent (no hidden reasoning). */
export function AgentDetailsPanel({ role, tasks }: { role: AgentRole; tasks: AgentTask[] }) {
  const info = AGENTS[role]
  const own = tasks.filter((t) => t.agent_role === role)
  const Icon = info.icon

  return (
    <Card className="shadow-xs">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon aria-hidden className="size-4 text-primary" /> {info.label}
        </CardTitle>
        <CardDescription>
          {info.description}
          {ROLE_PARENT_LABEL[role] && <> Reports to the {ROLE_PARENT_LABEL[role]}.</>}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {own.length === 0 && <p className="text-sm text-muted-foreground">No tasks yet.</p>}
        {own.map((task) => {
          const output = task.output ?? {}
          const list = (key: string) =>
            Array.isArray(output[key]) ? (output[key] as unknown[]).map(String) : []
          return (
            <div key={task.id} className="rounded-lg border bg-background/60 p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {task.task_key.split('.')[1]?.replace('_', ' ')}
                  {task.attempt > 1 && ` · attempt ${task.attempt}`}
                </span>
                <Badge variant="outline" className={STATUS_CLASS[task.status]}>
                  {task.status}
                </Badge>
              </div>
              <p className="mt-1 text-muted-foreground">{task.input_summary}</p>
              {output.summary && <p className="mt-2">{output.summary}</p>}
              {list('sub_questions').length > 0 && (
                <ul className="mt-2 list-disc space-y-0.5 pl-5 text-muted-foreground">
                  {list('sub_questions').map((q) => (
                    <li key={q}>{q}</li>
                  ))}
                </ul>
              )}
              {list('contradictions').length > 0 && (
                <p className="mt-2 text-xs text-warning">
                  Contradictions: {list('contradictions').join(' · ')}
                </p>
              )}
              {list('issues').length > 0 && (
                <p className="mt-2 text-xs text-warning">Issues: {list('issues').join(' · ')}</p>
              )}
              {task.error_message && (
                <p className="mt-2 text-xs text-destructive">
                  {task.error_code}: {task.error_message}
                </p>
              )}
              <div className="mt-2 flex gap-3 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <Clock aria-hidden className="size-3" /> {formatDuration(task.duration_ms)}
                </span>
                <span className="inline-flex items-center gap-1">
                  <Coins aria-hidden className="size-3" />
                  {formatNumber(task.input_tokens)} in / {formatNumber(task.output_tokens)} out
                </span>
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}
