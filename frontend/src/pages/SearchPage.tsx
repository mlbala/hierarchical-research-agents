import { FileSearch, Search } from 'lucide-react'
import { Fragment, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { EmptyState, ErrorState } from '@/components/common/status'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { useReportSearch } from '@/hooks/queries'
import { errorMessage } from '@/lib/api'
import { formatDate } from '@/lib/format'

/** Render PostgreSQL ts_headline «markers» as <mark>, safely (no HTML parsing). */
function Highlighted({ text }: { text: string }) {
  const parts = text.split(/(«[^»]*»)/g)
  return (
    <>
      {parts.map((part, index) =>
        part.startsWith('«') ? (
          <mark key={index} className="rounded bg-primary/15 px-0.5 text-foreground">
            {part.slice(1, -1)}
          </mark>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  )
}

export function SearchPage() {
  const [params, setParams] = useSearchParams()
  const [text, setText] = useState(params.get('q') ?? '')
  const [debounced, setDebounced] = useState(text)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebounced(text)
      setParams(text ? { q: text } : {}, { replace: true })
    }, 300)
    return () => window.clearTimeout(timer)
  }, [text, setParams])

  const results = useReportSearch(debounced)

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Search saved reports</h1>
        <p className="text-muted-foreground">Full-text search across titles, summaries and report text.</p>
      </header>
      <div className="relative">
        <Search aria-hidden className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Search, e.g. vouchers OR rebates"
          aria-label="Search saved reports"
          className="pl-9"
          maxLength={200}
          autoFocus
        />
      </div>

      {debounced.trim().length < 2 && (
        <EmptyState icon={FileSearch} title="Find past research">
          Type at least two characters. Quoted phrases and OR are supported.
        </EmptyState>
      )}
      {results.isLoading && <Skeleton className="h-24" />}
      {results.isError && (
        <ErrorState message={errorMessage(results.error)} onRetry={() => void results.refetch()} />
      )}
      {results.data && debounced.trim().length >= 2 && (
        <div className="space-y-3" aria-live="polite">
          <p className="text-sm text-muted-foreground">
            {results.data.total} report{results.data.total === 1 ? '' : 's'} found
          </p>
          <ul className="space-y-3">
            {results.data.items.map((hit) => (
              <li key={hit.report_id}>
                <Link
                  to={`/runs/${hit.run_id}`}
                  className="block rounded-xl border bg-card p-4 shadow-xs transition hover:shadow-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{hit.title}</span>
                    {hit.is_partial && <Badge variant="outline">partial</Badge>}
                    {hit.execution_mode === 'demo' && <Badge variant="outline">demo</Badge>}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {hit.question} · {formatDate(hit.created_at)}
                  </p>
                  <p className="mt-2 text-sm text-muted-foreground">
                    <Highlighted text={hit.snippet} />
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
