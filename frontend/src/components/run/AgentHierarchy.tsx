import { Ban, Check, CircleDashed, Loader2, SkipForward, XCircle } from 'lucide-react'
import { cn } from 'cn'

import { AGENTS, HIERARCHY, agentState, type AgentRole, type AgentState } from '@/lib/agents'
import type { AgentTask } from '@/lib/types'

const STATE_STYLE: Record<AgentState, { label: string; className: string; icon: typeof Check }> = {
  queued: { label: 'Waiting', className: 'border-dashed text-muted-foreground', icon: CircleDashed },
  working: {
    label: 'Working',
    className: 'border-primary bg-accent text-accent-foreground ring-4 ring-primary/15',
    icon: Loader2,
  },
  done: { label: 'Done', className: 'border-success/40 bg-success/5', icon: Check },
  skipped: { label: 'Skipped', className: 'border-dashed bg-muted/50 text-muted-foreground', icon: SkipForward },
  failed: { label: 'Failed', className: 'border-destructive/50 bg-destructive/5', icon: XCircle },
  stopped: { label: 'Stopped', className: 'border-warning/50 bg-warning/5', icon: Ban },
}

function AgentNode({
  role,
  tasks,
  selected,
  onSelect,
}: {
  role: AgentRole
  tasks: AgentTask[]
  selected: boolean
  onSelect: (role: AgentRole) => void
}) {
  const info = AGENTS[role]
  const state = agentState(role, tasks)
  const style = STATE_STYLE[state]
  const Icon = info.icon
  const StateIcon = style.icon
  const attempts = tasks.filter((t) => t.agent_role === role).length

  return (
    <button
      type="button"
      onClick={() => onSelect(role)}
      aria-pressed={selected}
      aria-label={`${info.label}: ${style.label}${attempts ? `, ${attempts} task(s)` : ''}`}
      title={info.description}
      data-state={state}
      className={cn(
        'relative flex w-full min-w-0 items-center gap-2 rounded-xl border bg-card px-3 py-2 text-left text-sm shadow-xs transition',
        'hover:shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
        style.className,
        state === 'working' && 'motion-safe:animate-pulse',
        selected && 'outline-2 outline-offset-2 outline-primary',
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0 text-primary" />
      <span className="min-w-0 flex-1 truncate font-medium">{info.label}</span>
      <StateIcon
        aria-hidden
        className={cn('size-3.5 shrink-0', state === 'working' && 'motion-safe:animate-spin')}
      />
      {attempts > 1 && (
        <span className="absolute -top-2 -right-2 rounded-full border bg-background px-1.5 text-[10px] tabular-nums">
          ×{attempts}
        </span>
      )}
    </button>
  )
}

/** The seven-agent hierarchy, drawn with HTML/CSS connectors. */
export function AgentHierarchy({
  tasks,
  selected,
  onSelect,
}: {
  tasks: AgentTask[]
  selected: AgentRole | null
  onSelect: (role: AgentRole) => void
}) {
  return (
    <div className="rounded-xl border bg-card/60 p-4" aria-label="Agent hierarchy" role="group">
      <div className="mx-auto max-w-xs">
        <AgentNode
          role="research_director"
          tasks={tasks}
          selected={selected === 'research_director'}
          onSelect={onSelect}
        />
      </div>
      {/* Connector from the director down to both teams. */}
      <div aria-hidden className="mx-auto h-4 w-px bg-border" />
      <div aria-hidden className="mx-auto h-px w-1/2 bg-border" />
      <div className="grid gap-4 sm:grid-cols-2">
        {HIERARCHY.map(({ lead, workers }) => (
          <div key={lead} className="flex flex-col items-stretch">
            <div aria-hidden className="mx-auto h-4 w-px bg-border" />
            <AgentNode role={lead} tasks={tasks} selected={selected === lead} onSelect={onSelect} />
            <div className="mt-2 ml-4 space-y-2 border-l pl-4">
              {workers.map((worker) => (
                <div key={worker} className="relative">
                  <span aria-hidden className="absolute top-1/2 -left-4 h-px w-4 bg-border" />
                  <AgentNode
                    role={worker}
                    tasks={tasks}
                    selected={selected === worker}
                    onSelect={onSelect}
                  />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
