import { ExternalLink, Globe2 } from 'lucide-react'
import { useState } from 'react'

import { EmptyState, ErrorState } from '@/components/common/status'
import { SourceKindBadge } from '@/components/run/SourceKindBadge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { useSourceDetail, useSources } from '@/hooks/queries'
import { errorMessage } from '@/lib/api'
import { formatDate, formatNumber, hostOf } from '@/lib/format'

/** Every source collected in the run, with retrieval details and full text on demand. */
export function SourcesPanel({ runId }: { runId: string }) {
  const sources = useSources(runId)
  const [openId, setOpenId] = useState<string | null>(null)
  const detail = useSourceDetail(runId, openId)

  if (sources.isLoading) return <Skeleton className="h-32" />
  if (sources.isError)
    return <ErrorState message={errorMessage(sources.error)} onRetry={() => void sources.refetch()} />
  if (!sources.data?.items.length) {
    return (
      <EmptyState icon={Globe2} title="No sources yet">
        Sources appear here as researchers find and read them.
      </EmptyState>
    )
  }

  return (
    <>
      <ul className="space-y-3">
        {sources.data.items.map((source) => (
          <li key={source.id} className="rounded-xl border bg-card p-4 shadow-xs">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="font-medium leading-snug">{source.title || hostOf(source.url)}</p>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {source.publisher ?? hostOf(source.url)} · retrieved {formatDate(source.retrieved_at)}
                  {source.published_at && ` · published ${formatDate(source.published_at)}`}
                </p>
              </div>
              <SourceKindBadge kind={source.content_kind} />
            </div>
            <p className="mt-2 line-clamp-3 text-sm text-muted-foreground">{source.excerpt}</p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button variant="outline" size="xs" onClick={() => setOpenId(source.id)}>
                View stored text ({formatNumber(source.content_length)} chars)
              </Button>
              <a
                href={source.url}
                target="_blank"
                rel="noopener noreferrer nofollow"
                className="inline-flex items-center gap-1 text-xs text-primary underline-offset-2 hover:underline"
              >
                {hostOf(source.url)} <ExternalLink aria-hidden className="size-3" />
              </a>
            </div>
          </li>
        ))}
      </ul>
      <Dialog open={!!openId} onOpenChange={(open) => !open && setOpenId(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{detail.data?.title ?? 'Source'}</DialogTitle>
            <DialogDescription>
              Stored text (untrusted page content, shown as plain text). Hash{' '}
              <code className="text-xs">{detail.data?.content_hash.slice(0, 12)}</code>
            </DialogDescription>
          </DialogHeader>
          <ScrollArea className="max-h-[60vh] rounded-md border bg-muted/40 p-3">
            {detail.isLoading ? (
              <Skeleton className="h-40" />
            ) : (
              // Rendered as text, never HTML: page content cannot inject markup.
              <pre className="font-sans text-sm whitespace-pre-wrap">{detail.data?.content_text}</pre>
            )}
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  )
}
