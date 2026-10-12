/** Small status, mode and state primitives shared across pages. */
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDashed,
  CirclePause,
  FlaskConical,
  Globe,
  Loader2,
  RefreshCw,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from 'cn'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { ExecutionMode, RunStatus } from '@/lib/types'

const STATUS: Record<RunStatus, { label: string; icon: LucideIcon; className: string }> = {
  queued: { label: 'Queued', icon: CircleDashed, className: 'text-muted-foreground bg-muted' },
  running: {
    label: 'Running',
    icon: Loader2,
    className: 'text-primary bg-accent [&_svg]:motion-safe:animate-spin',
  },
  completed: { label: 'Completed', icon: CheckCircle2, className: 'text-success bg-success/10' },
  partial: { label: 'Partial', icon: AlertTriangle, className: 'text-warning bg-warning/10' },
  failed: { label: 'Failed', icon: XCircle, className: 'text-destructive bg-destructive/10' },
  cancelled: { label: 'Cancelled', icon: Ban, className: 'text-muted-foreground bg-muted' },
  interrupted: {
    label: 'Interrupted',
    icon: CirclePause,
    className: 'text-warning bg-warning/10',
  },
}

export function StatusBadge({ status, className }: { status: RunStatus; className?: string }) {
  const meta = STATUS[status]
  const Icon = meta.icon
  return (
    <Badge variant="outline" className={cn('gap-1 border-transparent', meta.className, className)}>
      <Icon aria-hidden className="size-3.5" />
      {meta.label}
    </Badge>
  )
}

export function StatusDot({ status }: { status: RunStatus }) {
  const color = {
    queued: 'bg-muted-foreground/50',
    running: 'bg-primary motion-safe:animate-pulse',
    completed: 'bg-success',
    partial: 'bg-warning',
    failed: 'bg-destructive',
    cancelled: 'bg-muted-foreground/50',
    interrupted: 'bg-warning',
  }[status]
  return (
    <span className={cn('inline-block size-2 shrink-0 rounded-full', color)} aria-hidden />
  )
}

export function ModeBadge({ mode, ready = true }: { mode: ExecutionMode; ready?: boolean }) {
  if (mode === 'demo') {
    return (
      <Badge
        variant="outline"
        className="gap-1 border-info/30 bg-info/10 text-info"
        title="Demo mode uses fictional local fixtures and makes no external calls."
      >
        <FlaskConical aria-hidden className="size-3.5" /> Demo mode
      </Badge>
    )
  }
  return (
    <Badge
      variant="outline"
      className={cn(
        'gap-1',
        ready
          ? 'border-success/30 bg-success/10 text-success'
          : 'border-destructive/30 bg-destructive/10 text-destructive',
      )}
      title={ready ? 'Live mode: real web search and LLM calls.' : 'Live mode is not configured.'}
    >
      <Globe aria-hidden className="size-3.5" /> {ready ? 'Live mode' : 'Live: not configured'}
    </Badge>
  )
}

export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
}: {
  icon: LucideIcon
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed bg-card/50 px-6 py-12 text-center">
      <div className="rounded-full bg-accent p-3 text-accent-foreground">
        <Icon aria-hidden className="size-6" />
      </div>
      <h3 className="font-semibold">{title}</h3>
      {children && <div className="max-w-md text-sm text-muted-foreground">{children}</div>}
      {action}
    </div>
  )
}

export function ErrorState({
  title = 'Something went wrong',
  message,
  onRetry,
}: {
  title?: string
  message: string
  onRetry?: () => void
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-5"
    >
      <div className="flex items-center gap-2 font-medium text-destructive">
        <XCircle aria-hidden className="size-4" /> {title}
      </div>
      <p className="text-sm text-muted-foreground">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw aria-hidden /> Try again
        </Button>
      )}
    </div>
  )
}
