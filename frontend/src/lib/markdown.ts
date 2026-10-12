/** Pure helpers for safe report Markdown rendering (see SafeMarkdown.tsx). */

export const CITATION_HREF = /^#cite-(\d{1,3})$/
const SAFE_URL = /^(https?:|mailto:)/i

/** Turn plain `[3]` markers into in-page links the renderer can intercept. */
export function linkCitations(markdown: string): string {
  return markdown.replace(/\[(\d{1,3})\](?![(:\]])/g, '[[$1]](#cite-$1)')
}

/** URL allowlist: http(s), mailto and in-page citation anchors; everything else is dropped. */
export function safeUrl(url: string): string {
  const trimmed = url.trim()
  if (CITATION_HREF.test(trimmed) || SAFE_URL.test(trimmed)) return trimmed
  return ''
}

/** Remove the code-generated "## Sources" list: the UI renders source cards instead. */
export function bodyWithoutSources(markdown: string): string {
  const index = markdown.search(/^## Sources\s*$/m)
  return index === -1 ? markdown : markdown.slice(0, index)
}
