import {
  AlertTriangle,
  ChevronDown,
  CircleDot,
  Info,
  RotateCcw,
  Search,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import { cn } from 'cn'

import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { AGENTS, type AgentRole } from '@/lib/agents'
import type { RunEvent } from '@/lib/types'

function iconFor(event: RunEvent) {
  if (event.level === 'error') return <XCircle aria-hidden className="size-4 text-destructive" />
  if (event.level === 'warning') return <AlertTriangle aria-hidden className="size-4 text-warning" />
  if (event.event_type === 'tool_called') return <Search aria-hidden className="size-4 text-info" />
  if (event.event_type === 'retry') return <RotateCcw aria-hidden className="size-4 text-warning" />
  if (event.event_type === 'stage_changed')
    return <CircleDot aria-hidden className="size-4 text-primary" />
  return <Info aria-hidden className="size-4 text-muted-foreground" />
}

const time = new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' })

/**
 * Chronological, user-facing activity. Shows short action summaries and
 * results only, never hidden model reasoning.
 */
export function ActivityTimeline({ events }: { events: RunEvent[] }) {
  const [showDetail, setShowDetail] = useState(false)
  // Tool calls are numerous; group them unless the user asks for detail.
  const visible = showDetail ? events : events.filter((e) => e.event_type !== 'tool_called')
  const toolCount = events.length - events.filter((e) => e.event_type !== 'tool_called').length

  if (events.length === 0) {
    return <p className="py-6 text-sm text-muted-foreground">Waiting for the first update…</p>
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {events.length} events{toolCount ? ` · ${toolCount} tool calls` : ''}
        </p>
        <Button variant="ghost" size="xs" onClick={() => setShowDetail((v) => !v)}>
          {showDetail ? 'Hide tool calls' : 'Show tool calls'}
        </Button>
      </div>
      <ol className="relative space-y-1 border-l pl-5" aria-live="polite" aria-relevant="additions">
        {visible.map((event) => {
          const role = event.payload.role as AgentRole | undefined
          const attempt = Number(event.payload.attempt ?? 1)
          return (
            <li key={event.id} className="relative py-1">
              <span className="absolute top-1.5 -left-[1.6rem] rounded-full bg-background p-0.5">
                {iconFor(event)}
              </span>
              <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
                <time className="text-xs text-muted-foreground tabular-nums" dateTime={event.created_at}>
                  {time.format(new Date(event.created_at))}
                </time>
                {role && AGENTS[role] && (
                  <span className="text-xs font-medium text-primary">{AGENTS[role].short}</span>
                )}
                <span
                  className={cn(
                    event.event_type === 'stage_changed' && 'font-medium',
                    event.level === 'error' && 'text-destructive',
                  )}
                >
                  {event.message}
                </span>
              </div>
              {role && attempt > 1 && event.event_type === 'agent_started' && (
                <span className="text-xs text-muted-foreground">Attempt {attempt}</span>
              )}
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/** Collapsible wrapper used on the run page. */
export function CollapsibleTimeline({ events }: { events: RunEvent[] }) {
  const [open, setOpen] = useState(true)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <Button variant="ghost" size="sm" className="-ml-2">
          <ChevronDown
            aria-hidden
            className={cn('transition-transform', !open && '-rotate-90')}
          />
          Activity timeline
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-2">
        <ActivityTimeline events={events} />
      </CollapsibleContent>
    </Collapsible>
  )
}
