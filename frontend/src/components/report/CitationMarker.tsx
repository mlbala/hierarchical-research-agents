import { ExternalLink } from 'lucide-react'
import { cn } from 'cn'

import { SourceKindBadge } from '@/components/run/SourceKindBadge'
import { SUPPORT_STYLE } from '@/lib/support'
import { Badge } from '@/components/ui/badge'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { formatDate, hostOf } from '@/lib/format'
import type { Citation } from '@/lib/types'

/** A clickable [n] marker that previews the cited source and its evidence. */
export function CitationMarker({ number, citation }: { number: number; citation?: Citation }) {
  if (!citation) {
    return <sup className="text-muted-foreground">[{number}]</sup>
  }
  const { source } = citation
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Citation ${number}: ${source.title}`}
          className={cn(
            'mx-0.5 inline-flex -translate-y-1 items-center rounded px-1 font-sans text-[0.7em] leading-none font-semibold',
            'bg-accent text-accent-foreground hover:bg-primary hover:text-primary-foreground',
            'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring',
          )}
        >
          {number}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-3 font-sans text-sm" align="start">
        <div className="space-y-1">
          <p className="leading-snug font-medium">{source.title}</p>
          <p className="text-xs text-muted-foreground">
            {source.publisher ?? hostOf(source.url)} · retrieved {formatDate(source.retrieved_at)}
          </p>
          <SourceKindBadge kind={source.content_kind} />
        </div>
        {citation.evidence.length > 0 ? (
          <ul className="space-y-2">
            {citation.evidence.map((item, index) => (
              <li key={index} className="space-y-1">
                <Badge variant="outline" className={SUPPORT_STYLE[item.support_level]}>
                  {item.support_level}
                </Badge>
                <blockquote className="border-l-2 border-primary/40 pl-2 font-serif text-xs text-muted-foreground italic">
                  “{item.excerpt}”
                </blockquote>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No excerpt recorded for this source.</p>
        )}
        <a
          href={source.url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
        >
          Open {hostOf(source.url)} <ExternalLink aria-hidden className="size-3" />
        </a>
      </PopoverContent>
    </Popover>
  )
}
