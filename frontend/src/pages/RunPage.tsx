import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Ban,
  Clock,
  Coins,
  Cpu,
  FileText,
  Loader2,
  RefreshCw,
  RotateCcw,
  Wifi,
  WifiOff,
  Wrench,
} from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { cn } from 'cn'

import { ErrorState, ModeBadge, StatusBadge } from '@/components/common/status'
import { ReportView } from '@/components/report/ReportView'
import { ActivityTimeline } from '@/components/run/ActivityTimeline'
import { AgentDetailsPanel } from '@/components/run/AgentDetailsPanel'
import { AgentHierarchy } from '@/components/run/AgentHierarchy'
import { EvidencePanel } from '@/components/run/EvidencePanel'
import { SourcesPanel } from '@/components/run/SourcesPanel'
import { StageProgress } from '@/components/run/StageProgress'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { queryKeys, useReport, useRun, useTasks } from '@/hooks/queries'
import { useRunEvents, type ConnectionState } from '@/hooks/use-run-events'
import type { AgentRole } from '@/lib/agents'
import { ApiError, api, errorMessage, newIdempotencyKey } from '@/lib/api'
import { formatCost, formatDateTime, formatDuration, formatNumber } from '@/lib/format'
import { TERMINAL_STATUSES, type RunDetail } from '@/lib/types'

function useElapsed(run: RunDetail | undefined): number | null {
  const [now, setNow] = useState(() => Date.now())
  const running = run?.status === 'running'
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  if (!run?.started_at) return null
  const end = run.finished_at ? new Date(run.finished_at).getTime() : now
  return Math.max(0, end - new Date(run.started_at).getTime())
}

function Metric({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-xs">
      <span className="text-muted-foreground">{icon}</span>
      <div className="leading-tight">
        <div className="text-[11px] tracking-wide text-muted-foreground uppercase">{label}</div>
        <div className="text-sm font-medium tabular-nums">{children}</div>
      </div>
    </div>
  )
}

function ConnectionBadge({ state, onReconnect }: { state: ConnectionState; onReconnect: () => void }) {
  if (state === 'ended') return null
  if (state === 'lost') {
    return (
      <Button variant="outline" size="xs" onClick={onReconnect}>
        <WifiOff aria-hidden /> Live updates lost · Reconnect
      </Button>
    )
  }
  const live = state === 'live'
  return (
    <span
      className={cn('inline-flex items-center gap-1 text-xs', live ? 'text-success' : 'text-warning')}
      role="status"
    >
      {live ? <Wifi aria-hidden className="size-3.5" /> : <Loader2 aria-hidden className="size-3.5 motion-safe:animate-spin" />}
      {live ? 'Live' : state === 'reconnecting' ? 'Reconnecting…' : 'Connecting…'}
    </span>
  )
}

export function RunPage() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const run = useRun(runId)
  const { events, connection, reconnect } = useRunEvents(runId)
  const status = run.data?.status
  const active = !!status && !TERMINAL_STATUSES.has(status)
  const tasks = useTasks(runId, active)
  const report = useReport(runId, !!run.data?.report_id)
  const elapsed = useElapsed(run.data)
  const [selected, setSelected] = useState<AgentRole | null>(null)
  const [tab, setTab] = useState<string | null>(null)
  const rerunKey = useRef(newIdempotencyKey())

  // Follow whichever agent is working until the user picks one.
  const working = (tasks.data ?? []).findLast((t) => t.status === 'running')?.agent_role as
    | AgentRole
    | undefined
  const focusRole: AgentRole = selected ?? working ?? 'research_director'
  const currentTab = tab ?? (run.data?.report_id ? 'report' : 'activity')

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(runId),
    onSuccess: () => {
      toast.message('Cancellation requested. Collected evidence will be kept.')
      void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) })
    },
    onError: (error) => toast.error(errorMessage(error)),
  })
  const rerun = useMutation({
    mutationFn: () => api.rerun(runId, rerunKey.current),
    onSuccess: (next) => {
      rerunKey.current = newIdempotencyKey()
      void queryClient.invalidateQueries({ queryKey: queryKeys.recentRuns })
      void navigate(`/runs/${next.id}`)
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  if (run.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-24" />
        <Skeleton className="h-64" />
      </div>
    )
  }
  if (run.isError || !run.data) {
    const notFound = run.error instanceof ApiError && run.error.status === 404
    return (
      <ErrorState
        title={notFound ? 'Research run not found' : 'Could not load this run'}
        message={errorMessage(run.error)}
        onRetry={notFound ? undefined : () => void run.refetch()}
      />
    )
  }

  const data = run.data
  const taskList = tasks.data ?? []
  const workingCount = taskList.filter((t) => t.status === 'running').length
  const canRerun = TERMINAL_STATUSES.has(data.status)

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Link
          to={`/projects/${data.project_id}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft aria-hidden className="size-4" /> Back to project
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-2">
            <h1 className="text-xl font-semibold tracking-tight text-balance sm:text-2xl">
              {data.question}
            </h1>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={data.status} />
              <Badge variant="outline" className="capitalize">
                {data.depth}
              </Badge>
              <ModeBadge mode={data.execution_mode} />
              <span className="text-xs text-muted-foreground">
                Created {formatDateTime(data.created_at)}
              </span>
              {active && <ConnectionBadge state={connection} onReconnect={reconnect} />}
            </div>
          </div>
          <div className="flex gap-2">
            {active && (
              <Button
                variant="outline"
                onClick={() => cancel.mutate()}
                disabled={cancel.isPending || !!data.cancel_requested_at}
              >
                <Ban aria-hidden /> {data.cancel_requested_at ? 'Cancelling…' : 'Cancel'}
              </Button>
            )}
            {canRerun && (
              <Button variant="outline" onClick={() => rerun.mutate()} disabled={rerun.isPending}>
                <RotateCcw aria-hidden /> Re-run
              </Button>
            )}
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Metric icon={<Clock className="size-4" />} label="Elapsed">
            {elapsed === null ? 'not started' : formatDuration(elapsed)}
          </Metric>
          <Metric icon={<Cpu className="size-4" />} label="Model calls">
            {data.llm_call_count}
            {data.settings_snapshot.limits?.max_llm_calls !== undefined &&
              ` / ${data.settings_snapshot.limits.max_llm_calls}`}
          </Metric>
          <Metric icon={<Wrench className="size-4" />} label="Tool calls">
            {data.tool_call_count}
          </Metric>
          <Metric icon={<Coins className="size-4" />} label="Tokens in / out">
            {formatNumber(data.input_tokens)} / {formatNumber(data.output_tokens)}
          </Metric>
          <Metric icon={<FileText className="size-4" />} label="Est. LLM cost">
            {formatCost(data.estimated_cost_usd, data.execution_mode)}
          </Metric>
        </div>
      </div>

      {data.status === 'queued' && (
        <Alert>
          <Loader2 aria-hidden className="motion-safe:animate-spin" />
          <AlertTitle>Waiting in the queue</AlertTitle>
          <AlertDescription>
            The worker runs one research job at a time. This run starts automatically; you can
            close this page and come back later.
          </AlertDescription>
        </Alert>
      )}
      {data.status === 'failed' && (
        <Alert variant="destructive">
          <AlertTitle>Research failed{data.error_code ? ` (${data.error_code})` : ''}</AlertTitle>
          <AlertDescription>
            <p>{data.error_message ?? 'The run stopped because of an error.'}</p>
            <p>Any evidence collected so far is still available in the Sources and Evidence tabs.</p>
          </AlertDescription>
        </Alert>
      )}
      {data.status === 'interrupted' && (
        <Alert className="border-warning/40 bg-warning/5">
          <RefreshCw aria-hidden className="text-warning" />
          <AlertTitle>Run interrupted</AlertTitle>
          <AlertDescription>
            {data.error_message ?? 'The server stopped before this run finished.'} Runs are not
            resumed automatically: use Re-run to start a fresh run with the same question.
          </AlertDescription>
        </Alert>
      )}
      {data.status === 'cancelled' && (
        <Alert>
          <Ban aria-hidden />
          <AlertTitle>Cancelled</AlertTitle>
          <AlertDescription>Evidence collected before cancellation was kept.</AlertDescription>
        </Alert>
      )}
      {data.warnings.length > 0 && (
        <Alert className="border-warning/30">
          <AlertTitle>{data.warnings.length} warning(s)</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-5">
              {data.warnings.map((w) => (
                <li key={w.message}>{w.message}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-6">
          <section aria-labelledby="progress-heading" className="space-y-3">
            <h2 id="progress-heading" className="sr-only">
              Progress
            </h2>
            <StageProgress events={events} status={data.status} />
            <AgentHierarchy tasks={taskList} selected={selected} onSelect={setSelected} />
            <p className="text-xs text-muted-foreground" aria-live="polite">
              {active
                ? `${workingCount} agent task(s) working · at most ${
                    data.settings_snapshot.max_parallel_agent_tasks ?? 2
                  } run in parallel`
                : `${taskList.length} agent tasks recorded`}
            </p>
          </section>

          <Tabs value={currentTab} onValueChange={setTab}>
            <TabsList>
              {data.report_id && <TabsTrigger value="report">Report</TabsTrigger>}
              <TabsTrigger value="activity">Activity</TabsTrigger>
              <TabsTrigger value="sources">Sources ({data.source_count})</TabsTrigger>
              <TabsTrigger value="evidence">Evidence ({data.finding_count})</TabsTrigger>
            </TabsList>
            {data.report_id && (
              <TabsContent value="report" className="pt-4">
                {report.isLoading && <Skeleton className="h-96" />}
                {report.isError && (
                  <ErrorState message={errorMessage(report.error)} onRetry={() => void report.refetch()} />
                )}
                {report.data && <ReportView report={report.data} run={data} />}
              </TabsContent>
            )}
            <TabsContent value="activity" className="pt-4">
              <ActivityTimeline events={events} />
            </TabsContent>
            <TabsContent value="sources" className="pt-4">
              <SourcesPanel runId={runId} />
            </TabsContent>
            <TabsContent value="evidence" className="pt-4">
              <EvidencePanel runId={runId} />
            </TabsContent>
          </Tabs>
        </div>

        <aside className="space-y-4 xl:sticky xl:top-6 xl:self-start" aria-label="Agent details">
          <AgentDetailsPanel role={focusRole} tasks={taskList} />
          {data.plan?.objective && (
            <Card className="shadow-xs">
              <CardHeader>
                <CardTitle className="text-base">Research plan</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <p className="text-muted-foreground">{data.plan.objective}</p>
                {data.plan.sub_questions && (
                  <ol className="list-decimal space-y-1 pl-5">
                    {data.plan.sub_questions.map((q) => (
                      <li key={q}>{q}</li>
                    ))}
                  </ol>
                )}
              </CardContent>
            </Card>
          )}
        </aside>
      </div>
    </div>
  )
}

/** Remount the run page per run so live-event state never leaks between runs. */
export function KeyedRunPage() {
  const { runId = '' } = useParams()
  return <RunPage key={runId} />
}
