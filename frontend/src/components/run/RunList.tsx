import { ChevronRight } from 'lucide-react'
import { Link } from 'react-router'

import { StatusBadge } from '@/components/common/status'
import { Badge } from '@/components/ui/badge'
import { formatRelative } from '@/lib/format'
import type { RunSummary } from '@/lib/types'

/** Compact, keyboard-navigable list of research runs. */
export function RunList({ runs }: { runs: RunSummary[] }) {
  return (
    <ul className="divide-y overflow-hidden rounded-xl border bg-card shadow-xs">
      {runs.map((run) => (
        <li key={run.id}>
          <Link
            to={`/runs/${run.id}`}
            className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none"
          >
            <div className="min-w-0 flex-1 space-y-1">
              <p className="line-clamp-2 text-sm font-medium">{run.question}</p>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <StatusBadge status={run.status} />
                {run.is_partial && <Badge variant="outline">partial report</Badge>}
                <span className="capitalize">{run.depth}</span>
                <span>·</span>
                <span>{run.execution_mode}</span>
                <span>·</span>
                <time dateTime={run.created_at}>{formatRelative(run.created_at)}</time>
              </div>
            </div>
            <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          </Link>
        </li>
      ))}
    </ul>
  )
}
