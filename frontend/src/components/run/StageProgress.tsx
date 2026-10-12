import { Check, Loader2 } from 'lucide-react'
import { cn } from 'cn'

import { STAGES } from '@/lib/agents'
import type { RunEvent, RunStatus } from '@/lib/types'

/**
 * Stage-based progress derived from `stage_changed` events. There is no
 * percentage: the workflow has bounded but variable loops, so a percentage
 * would be invented.
 */
export function StageProgress({ events, status }: { events: RunEvent[]; status: RunStatus }) {
  const stageEvents = events.filter((e) => e.event_type === 'stage_changed')
  const current = stageEvents.at(-1)?.payload.stage as string | undefined
  const pass = stageEvents.reduce(
    (max, e) => Math.max(max, Number(e.payload.research_pass ?? 1)),
    1,
  )
  const currentIndex = STAGES.findIndex((s) => s.key === current)
  const finishedOk = status === 'completed' || status === 'partial'
  const stopped = ['failed', 'cancelled', 'interrupted'].includes(status)

  return (
    <ol aria-label="Workflow stages" className="flex flex-wrap gap-x-1 gap-y-2">
      {STAGES.map((stage, index) => {
        // A follow-up research pass moves the marker back to "Research".
        const done = finishedOk || index < currentIndex
        const active = !finishedOk && !stopped && index === currentIndex
        const halted = stopped && index === currentIndex
        return (
          <li key={stage.key} className="flex items-center gap-1">
            <span
              aria-current={active ? 'step' : undefined}
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs',
                done && 'border-success/30 bg-success/10 text-success',
                active && 'border-primary/40 bg-accent font-medium text-accent-foreground',
                halted && 'border-destructive/30 bg-destructive/10 text-destructive',
                !done && !active && !halted && 'text-muted-foreground',
              )}
            >
              {done && <Check aria-hidden className="size-3" />}
              {active && <Loader2 aria-hidden className="size-3 motion-safe:animate-spin" />}
              {stage.label}
              {stage.key === 'researching' && pass > 1 && (
                <span className="text-[10px] opacity-80">· pass {pass}</span>
              )}
            </span>
            {index < STAGES.length - 1 && (
              <span aria-hidden className="h-px w-2 bg-border" />
            )}
          </li>
        )
      })}
    </ol>
  )
}
