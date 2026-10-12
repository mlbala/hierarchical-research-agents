import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { SafeMarkdown } from '@/components/report/SafeMarkdown'
import { bodyWithoutSources, linkCitations, safeUrl } from '@/lib/markdown'

describe('safe report Markdown', () => {
  it('drops raw HTML instead of rendering it', () => {
    const { container } = render(
      <SafeMarkdown>{'Hello <script>alert(1)</script> <b onclick="x()">bold</b> <iframe src="https://evil.example"></iframe>'}</SafeMarkdown>,
    )
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('[onclick]')).toBeNull()
    expect(container.textContent).toContain('Hello')
  })

  it('removes unsafe link targets but keeps safe ones', () => {
    const { container } = render(
      <SafeMarkdown>
        {'[bad](javascript:alert(1)) [data](data:text/html,x) [good](https://example.com/a) [mail](mailto:a@b.example)'}
      </SafeMarkdown>,
    )
    const hrefs = Array.from(container.querySelectorAll('a')).map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(['https://example.com/a', 'mailto:a@b.example'])
    for (const a of container.querySelectorAll('a')) {
      expect(a.getAttribute('rel')).toBe('noopener noreferrer nofollow')
    }
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('never loads remote images', () => {
    const { container } = render(<SafeMarkdown>{'![pixel](https://tracker.example/p.png)'}</SafeMarkdown>)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('image not loaded')
  })

  it('renders citation markers through the citation renderer', () => {
    const { container } = render(
      <SafeMarkdown renderCitation={(n) => <button data-cite={n}>{n}</button>}>
        {'Claim one [1] and two [2][3]. A [link](https://example.com).'}
      </SafeMarkdown>,
    )
    const cites = Array.from(container.querySelectorAll('button[data-cite]')).map((b) =>
      b.getAttribute('data-cite'),
    )
    expect(cites).toEqual(['1', '2', '3'])
  })

  it('has pure helpers with safe defaults', () => {
    expect(linkCitations('A [1] B [12](https://x.example)')).toBe('A [[1]](#cite-1) B [12](https://x.example)')
    expect(safeUrl('javascript:alert(1)')).toBe('')
    expect(safeUrl(' https://ok.example ')).toBe('https://ok.example')
    expect(safeUrl('#cite-4')).toBe('#cite-4')
    expect(safeUrl('#elsewhere')).toBe('')
    expect(bodyWithoutSources('## A\ntext\n## Sources\n1. x')).toBe('## A\ntext\n')
  })
})
