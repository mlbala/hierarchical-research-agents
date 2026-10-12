import { Quote } from 'lucide-react'
import { cn } from 'cn'

import { EmptyState, ErrorState } from '@/components/common/status'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { useFindings, useSources } from '@/hooks/queries'
import { AGENTS, type AgentRole } from '@/lib/agents'
import { errorMessage } from '@/lib/api'
import { hostOf } from '@/lib/format'
import { SUPPORT_STYLE } from '@/lib/support'

/** Findings with their verbatim excerpts, support level and review outcome. */
export function EvidencePanel({ runId }: { runId: string }) {
  const findings = useFindings(runId)
  const sources = useSources(runId)
  const byId = new Map(sources.data?.items.map((s) => [s.id, s]))

  if (findings.isLoading) return <Skeleton className="h-32" />
  if (findings.isError)
    return <ErrorState message={errorMessage(findings.error)} onRetry={() => void findings.refetch()} />
  if (!findings.data?.items.length) {
    return (
      <EmptyState icon={Quote} title="No evidence yet">
        Findings appear once researchers extract verified excerpts from sources.
      </EmptyState>
    )
  }

  return (
    <ul className="space-y-3">
      {findings.data.items.map((finding) => {
        const source = byId.get(finding.source_id)
        return (
          <li
            key={finding.id}
            className={cn(
              'rounded-xl border bg-card p-4 shadow-xs',
              finding.review_status === 'rejected' && 'opacity-60',
            )}
          >
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant="outline" className={SUPPORT_STYLE[finding.support_level]}>
                {finding.support_level}
              </Badge>
              {finding.review_status === 'rejected' && (
                <Badge variant="outline" className="text-destructive">
                  Rejected by review
                </Badge>
              )}
              {finding.review_status === 'pending' && (
                <Badge variant="outline" className="text-muted-foreground">
                  Awaiting review
                </Badge>
              )}
              <span className="text-xs text-muted-foreground">
                {AGENTS[finding.agent_role as AgentRole]?.label ?? finding.agent_role}
              </span>
            </div>
            <p className="mt-2 text-sm font-medium">{finding.claim}</p>
            <blockquote className="mt-2 border-l-2 border-primary/40 pl-3 font-serif text-sm text-muted-foreground italic">
              “{finding.evidence_excerpt}”
            </blockquote>
            {source && (
              <p className="mt-2 text-xs text-muted-foreground">
                Source: {source.title} ({source.publisher ?? hostOf(source.url)})
              </p>
            )}
          </li>
        )
      })}
    </ul>
  )
}
