import { useMutation } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  Copy,
  Download,
  ExternalLink,
  FlaskConical,
  ShieldCheck,
} from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { CitationMarker } from '@/components/report/CitationMarker'
import { SafeMarkdown } from '@/components/report/SafeMarkdown'
import { SourceKindBadge } from '@/components/run/SourceKindBadge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { api, errorMessage } from '@/lib/api'
import { formatDate, formatDateTime, hostOf } from '@/lib/format'
import { bodyWithoutSources } from '@/lib/markdown'
import type { Report, RunDetail } from '@/lib/types'

export function ReportView({ report, run }: { report: Report; run: RunDetail }) {
  const [copied, setCopied] = useState(false)
  const byNumber = new Map(report.citations.map((c) => [c.number, c]))
  const renderCitation = (n: number) => <CitationMarker number={n} citation={byNumber.get(n)} />

  const copy = useMutation({
    mutationFn: async () => {
      const text = await api.getReportMarkdown(report.run_id)
      await navigator.clipboard.writeText(text)
    },
    onSuccess: () => {
      setCopied(true)
      toast.success('Markdown copied to the clipboard.')
      window.setTimeout(() => setCopied(false), 2000)
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  return (
    <article className="space-y-6" aria-labelledby="report-title">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 id="report-title" className="text-2xl font-semibold tracking-tight text-balance">
            {report.title}
          </h2>
          <p className="text-sm text-muted-foreground">
            Saved {formatDateTime(report.created_at)} · {report.citations.length} cited sources
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => copy.mutate()} disabled={copy.isPending}>
            {copied ? <Check aria-hidden /> : <Copy aria-hidden />} Copy Markdown
          </Button>
          <Button asChild size="sm">
            <a href={api.reportMarkdownUrl(report.run_id)} download>
              <Download aria-hidden /> Download .md
            </a>
          </Button>
        </div>
      </div>

      {report.execution_mode === 'demo' && (
        <Alert className="border-info/30 bg-info/5">
          <FlaskConical aria-hidden className="text-info" />
          <AlertTitle>Demo report</AlertTitle>
          <AlertDescription>
            Produced from fictional fixture data to demonstrate the workflow. It is not real
            research.
          </AlertDescription>
        </Alert>
      )}
      {report.is_partial && (
        <Alert className="border-warning/40 bg-warning/5">
          <AlertTriangle aria-hidden className="text-warning" />
          <AlertTitle>Partial report</AlertTitle>
          <AlertDescription>
            Some planned research could not be completed, so parts of the question may be
            unanswered. The <em>Limitations and unresolved questions</em> section below explains
            the missing coverage.
          </AlertDescription>
        </Alert>
      )}

      <section aria-labelledby="summary-heading" className="rounded-xl border bg-card p-5 shadow-xs">
        <h3 id="summary-heading" className="mb-2 text-sm font-semibold tracking-wide text-primary uppercase">
          Executive summary
        </h3>
        <SafeMarkdown className="report-prose" renderCitation={renderCitation}>
          {report.executive_summary}
        </SafeMarkdown>
        {run.handoff?.summary && (
          <div className="mt-4 flex gap-2 rounded-lg bg-muted/60 p-3 text-sm">
            <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-primary" />
            <p>
              <span className="font-medium">Director&rsquo;s handoff</span>
              {run.handoff.confidence && (
                <Badge variant="outline" className="mx-2">
                  {run.handoff.confidence} confidence
                </Badge>
              )}
              <span className="text-muted-foreground">{run.handoff.summary}</span>
            </p>
          </div>
        )}
      </section>

      <SafeMarkdown className="report-prose" renderCitation={renderCitation}>
        {bodyWithoutSources(report.body_markdown)}
      </SafeMarkdown>

      <section aria-labelledby="sources-heading" className="space-y-3">
        <h3 id="sources-heading" className="border-b pb-2 text-xl font-semibold tracking-tight">
          Sources
        </h3>
        <ol className="grid gap-3 md:grid-cols-2">
          {report.citations.map(({ number, source }) => (
            <li key={source.id} id={`source-${number}`} className="rounded-xl border bg-card p-4 shadow-xs">
              <div className="flex items-start gap-3">
                <span className="rounded-md bg-accent px-2 py-0.5 text-sm font-semibold text-accent-foreground tabular-nums">
                  {number}
                </span>
                <div className="min-w-0 flex-1 space-y-1">
                  <p className="leading-snug font-medium">{source.title}</p>
                  <p className="text-xs text-muted-foreground">
                    {source.publisher ?? hostOf(source.url)}
                    {source.published_at && ` · published ${formatDate(source.published_at)}`}
                    {' · '}retrieved {formatDate(source.retrieved_at)}
                  </p>
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <SourceKindBadge kind={source.content_kind} />
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noopener noreferrer nofollow"
                      className="inline-flex min-w-0 items-center gap-1 truncate text-xs text-primary hover:underline"
                    >
                      {hostOf(source.url)} <ExternalLink aria-hidden className="size-3 shrink-0" />
                    </a>
                  </div>
                </div>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </article>
  )
}
