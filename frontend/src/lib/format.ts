/** Small, dependency-free formatting helpers. */

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
})
const dateOnly = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
const integer = new Intl.NumberFormat()

export function formatDateTime(value: string | null | undefined): string {
  return value ? dateTime.format(new Date(value)) : '—'
}

export function formatDate(value: string | null | undefined): string {
  return value ? dateOnly.format(new Date(value)) : '—'
}

export function formatRelative(value: string | null | undefined, now = Date.now()): string {
  if (!value) return '—'
  const seconds = Math.round((new Date(value).getTime() - now) / 1000)
  const abs = Math.abs(seconds)
  if (abs < 60) return relative.format(seconds, 'second')
  if (abs < 3600) return relative.format(Math.round(seconds / 60), 'minute')
  if (abs < 86_400) return relative.format(Math.round(seconds / 3600), 'hour')
  return relative.format(Math.round(seconds / 86_400), 'day')
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
}

export function formatNumber(value: number): string {
  return integer.format(value)
}

/** Unknown cost stays "unknown"; it is never shown as zero. */
export function formatCost(value: string | null, mode: 'live' | 'demo'): string {
  if (mode === 'demo') return '$0 (demo)'
  if (value === null) return 'unknown'
  const amount = Number(value)
  return amount < 0.01 ? '< $0.01' : `$${amount.toFixed(2)}`
}

export function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}
