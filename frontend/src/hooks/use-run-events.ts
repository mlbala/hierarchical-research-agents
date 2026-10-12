/**
 * Live run progress over Server-Sent Events.
 *
 * The server persists every event before streaming it, so this hook can
 * always rebuild the full timeline from the database: on first connect it
 * replays from the beginning, and on reconnect the browser's EventSource sends
 * `Last-Event-ID` automatically. If the browser gives up (the stream is
 * closed), `reconnect()` resumes with `?after=<last id>`. Closing the page
 * never cancels the research run.
 *
 * Callers mount this per run (the run page is keyed by run id), so state
 * never needs resetting when navigating between runs.
 */
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { queryKeys } from '@/hooks/queries'
import { API_BASE } from '@/lib/api'
import type { RunEvent } from '@/lib/types'

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'ended' | 'lost'

const TERMINAL_EVENTS = new Set([
  'run_completed',
  'run_partial',
  'run_failed',
  'run_cancelled',
  'run_interrupted',
])

export function useRunEvents(runId: string) {
  const queryClient = useQueryClient()
  const [events, setEvents] = useState<RunEvent[]>([])
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [attempt, setAttempt] = useState(0)
  const lastIdRef = useRef(0)
  const refreshTimer = useRef<number | null>(null)

  // Coalesce bursts of events into one refetch of the run's server state.
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current !== null) return
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null
      void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) })
    }, 400)
  }, [queryClient, runId])

  useEffect(() => {
    const after = lastIdRef.current
    const source = new EventSource(
      `${API_BASE}/runs/${runId}/events/stream${after ? `?after=${after}` : ''}`,
    )

    source.onopen = () => setConnection('live')
    source.addEventListener('run_event', (message) => {
      const event = JSON.parse((message as MessageEvent<string>).data) as RunEvent
      if (event.id <= lastIdRef.current) return // already seen (replay overlap)
      lastIdRef.current = event.id
      setEvents((previous) => [...previous, event])
      setConnection('live')
      scheduleRefresh()
      if (event.event_type === 'report_ready') {
        void queryClient.invalidateQueries({ queryKey: queryKeys.report(runId) })
      }
      if (TERMINAL_EVENTS.has(event.event_type)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.recentRuns })
        void queryClient.invalidateQueries({ queryKey: ['project'] })
      }
    })
    source.addEventListener('heartbeat', () => setConnection('live'))
    source.addEventListener('end', () => {
      setConnection('ended')
      source.close()
      void queryClient.invalidateQueries({ queryKey: ['run', runId] })
    })
    source.onerror = () => {
      // CONNECTING means the browser is already retrying with Last-Event-ID.
      setConnection(source.readyState === EventSource.CLOSED ? 'lost' : 'reconnecting')
    }
    return () => source.close()
  }, [runId, attempt, queryClient, scheduleRefresh])

  useEffect(
    () => () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    },
    [],
  )

  const reconnect = useCallback(() => {
    setConnection('reconnecting')
    setAttempt((n) => n + 1)
  }, [])
  return { events, connection, reconnect }
}
