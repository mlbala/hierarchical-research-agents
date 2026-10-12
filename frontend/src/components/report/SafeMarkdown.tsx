/**
 * Safe Markdown rendering for model-written report text.
 *
 * Defences (in addition to the server-side sanitiser):
 *  - raw HTML in the Markdown is dropped (`skipHtml`), never rendered;
 *  - URLs pass through an allowlist (http, https, mailto, in-page citations);
 *  - images are never loaded: they render as a text label;
 *  - external links open with rel="noopener noreferrer nofollow".
 */
import { ImageOff } from 'lucide-react'
import type { ReactNode } from 'react'
import Markdown, { type Components } from 'react-markdown'

import { CITATION_HREF, linkCitations, safeUrl } from '@/lib/markdown'

export function SafeMarkdown({
  children,
  renderCitation,
  className,
}: {
  children: string
  renderCitation?: (n: number) => ReactNode
  className?: string
}) {
  const components: Components = {
    a: ({ href, children: content }) => {
      const citation = href ? CITATION_HREF.exec(href) : null
      if (citation && renderCitation) return <>{renderCitation(Number(citation[1]))}</>
      if (!href) return <span>{content}</span>
      return (
        <a href={href} target="_blank" rel="noopener noreferrer nofollow">
          {content}
        </a>
      )
    },
    img: ({ alt }) => (
      <span className="inline-flex items-center gap-1 rounded bg-muted px-1.5 text-xs text-muted-foreground">
        <ImageOff aria-hidden className="size-3" /> image not loaded{alt ? `: ${alt}` : ''}
      </span>
    ),
  }
  return (
    <div className={className}>
      <Markdown skipHtml urlTransform={safeUrl} components={components}>
        {linkCitations(children)}
      </Markdown>
    </div>
  )
}
