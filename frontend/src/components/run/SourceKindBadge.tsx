import { FileCheck2, FlaskConical, TextQuote } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { ContentKind } from '@/lib/types'

/** Makes it explicit whether a page was actually read or only its snippet seen. */
export function SourceKindBadge({ kind }: { kind: ContentKind }) {
  if (kind === 'fetched_page') {
    return (
      <Badge variant="outline" className="gap-1 text-success" title="The page itself was retrieved and read.">
        <FileCheck2 aria-hidden className="size-3" /> Page read
      </Badge>
    )
  }
  if (kind === 'search_snippet') {
    return (
      <Badge
        variant="outline"
        className="gap-1 text-warning"
        title="Only the search-result snippet was available; the page was not read."
      >
        <TextQuote aria-hidden className="size-3" /> Snippet only
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="gap-1 text-info" title="Fictional demo fixture.">
      <FlaskConical aria-hidden className="size-3" /> Demo fixture
    </Badge>
  )
}
