import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Link2, Loader2, Sparkles } from 'lucide-react'
import { useMemo, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { cn } from 'cn'

import { ModeBadge } from '@/components/common/status'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { queryKeys, useConfig } from '@/hooks/queries'
import { ApiError, api, newIdempotencyKey } from '@/lib/api'
import type { Depth } from '@/lib/types'

const MAX_QUESTION = 2000
const MAX_CONTEXT = 4000
const MAX_URLS = 5

function parseUrls(text: string): { urls: string[]; invalid: string[] } {
  const lines = text
    .split(/\s+/)
    .map((line) => line.trim())
    .filter(Boolean)
  const urls: string[] = []
  const invalid: string[] = []
  for (const line of lines) {
    try {
      const url = new URL(line)
      if (url.protocol === 'http:' || url.protocol === 'https:') urls.push(url.toString())
      else invalid.push(line)
    } catch {
      invalid.push(line)
    }
  }
  return { urls, invalid }
}

export function ResearchForm({ projectId }: { projectId: string }) {
  const config = useConfig()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [question, setQuestion] = useState('')
  const [context, setContext] = useState('')
  const [urlText, setUrlText] = useState('')
  const [depth, setDepth] = useState<Depth>('standard')
  const [touched, setTouched] = useState(false)
  // One key per submission attempt: retrying after a network error reuses it,
  // so a double-submit can never create two runs.
  const idempotencyKey = useRef(newIdempotencyKey())

  const { urls, invalid } = useMemo(() => parseUrls(urlText), [urlText])
  const questionError =
    question.trim().length < 10
      ? 'Please write a question of at least 10 characters.'
      : question.length > MAX_QUESTION
        ? `Keep the question under ${MAX_QUESTION} characters.`
        : null
  const urlError =
    invalid.length > 0
      ? `Not a valid http(s) URL: ${invalid[0]}`
      : urls.length > MAX_URLS
        ? `At most ${MAX_URLS} reference URLs.`
        : null

  const liveBlocked = config.data?.execution_mode === 'live' && !config.data.live_ready
  const limits = config.data?.limits[depth]

  const start = useMutation({
    mutationFn: () =>
      api.createRun(
        projectId,
        {
          question: question.trim(),
          context: context.trim() || null,
          input_urls: urls,
          depth,
        },
        idempotencyKey.current,
      ),
    onSuccess: (run) => {
      idempotencyKey.current = newIdempotencyKey()
      void queryClient.invalidateQueries({ queryKey: queryKeys.projectRuns(projectId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.recentRuns })
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects })
      void navigate(`/runs/${run.id}`)
    },
    onError: (error) => {
      // Validation/config errors mean the next attempt is a different request.
      if (error instanceof ApiError && !error.retryable) {
        idempotencyKey.current = newIdempotencyKey()
      }
    },
  })

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    setTouched(true)
    if (questionError || urlError || liveBlocked || start.isPending) return
    start.mutate()
  }

  const serverError = start.error instanceof ApiError ? start.error : null
  const fieldError = (field: string) =>
    serverError?.details.find((detail) => detail.field.startsWith(field))?.message

  return (
    <Card className="shadow-sm">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-lg">New research</CardTitle>
          {config.data && (
            <ModeBadge mode={config.data.execution_mode} ready={config.data.live_ready} />
          )}
        </div>
        <CardDescription>
          Seven agents plan, research, review and write a cited report. Progress appears live.
          {config.data?.execution_mode === 'live' && config.data.llm_model && (
            <span className="mt-1 block text-xs">
              Model: <code>{config.data.llm_model}</code> ({config.data.llm_provider}) · web
              search: {config.data.search_provider}
            </span>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} noValidate className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="question">Research question</Label>
            <Textarea
              id="question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onBlur={() => setTouched(true)}
              rows={3}
              maxLength={MAX_QUESTION}
              placeholder="e.g. Do e-bike voucher programmes reduce car trips in mid-sized cities?"
              aria-invalid={touched && !!(questionError || fieldError('question'))}
              aria-describedby="question-help"
              className="resize-y text-base"
            />
            <div id="question-help" className="flex justify-between text-xs">
              <span className={cn('text-destructive', !(touched && (questionError || fieldError('question'))) && 'invisible')}>
                {fieldError('question') ?? questionError ?? '.'}
              </span>
              <span className="text-muted-foreground tabular-nums">
                {question.length}/{MAX_QUESTION}
              </span>
            </div>
          </div>

          <Collapsible>
            <CollapsibleTrigger asChild>
              <Button type="button" variant="ghost" size="sm" className="group -ml-2">
                <ChevronDown
                  aria-hidden
                  className="transition-transform group-data-[state=open]:rotate-180"
                />
                Context and reference URLs
                <span className="text-muted-foreground">(optional)</span>
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="space-y-4 pt-3">
              <div className="space-y-2">
                <Label htmlFor="context">Context</Label>
                <Textarea
                  id="context"
                  value={context}
                  onChange={(e) => setContext(e.target.value)}
                  rows={3}
                  maxLength={MAX_CONTEXT}
                  placeholder="Who is asking, constraints, what you already know…"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="urls" className="flex items-center gap-1.5">
                  <Link2 aria-hidden className="size-3.5" /> Public reference URLs
                </Label>
                <Textarea
                  id="urls"
                  value={urlText}
                  onChange={(e) => setUrlText(e.target.value)}
                  rows={2}
                  placeholder="One per line, up to 5. Only public http(s) pages are read."
                  aria-invalid={!!(urlError || fieldError('input_urls'))}
                  className="font-mono text-sm"
                />
                {(urlError || fieldError('input_urls')) && (
                  <p className="text-xs text-destructive">{fieldError('input_urls') ?? urlError}</p>
                )}
              </div>
            </CollapsibleContent>
          </Collapsible>

          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Depth</legend>
            <ToggleGroup
              type="single"
              variant="outline"
              value={depth}
              onValueChange={(value) => value && setDepth(value as Depth)}
              aria-label="Research depth"
            >
              <ToggleGroupItem value="quick" className="px-4">
                Quick
              </ToggleGroupItem>
              <ToggleGroupItem value="standard" className="px-4">
                Standard
              </ToggleGroupItem>
            </ToggleGroup>
            <p className="text-xs text-muted-foreground">
              {depth === 'quick'
                ? 'One research pass, no revision loop.'
                : 'Allows one follow-up research pass and one draft revision.'}
              {limits &&
                ` Limits: ${limits.max_llm_calls} model calls, ${limits.max_search_calls} searches, ` +
                  `${limits.max_sources} sources, ${Math.round(limits.deadline_seconds / 60)} min.`}
            </p>
          </fieldset>

          {liveBlocked && (
            <Alert variant="destructive">
              <AlertTitle>Live research is not configured</AlertTitle>
              <AlertDescription>
                Missing: {config.data?.issues.filter((i) => i.severity === 'error').map((i) => i.key).join(', ')}.
                Ask the operator to update the server configuration, or enable DEMO_MODE.
              </AlertDescription>
            </Alert>
          )}
          {serverError && !serverError.details.length && (
            <Alert variant="destructive" role="alert">
              <AlertTitle>Could not start research</AlertTitle>
              <AlertDescription>{serverError.message}</AlertDescription>
            </Alert>
          )}

          <Button type="submit" size="lg" disabled={start.isPending || liveBlocked}>
            {start.isPending ? (
              <Loader2 aria-hidden className="motion-safe:animate-spin" />
            ) : (
              <Sparkles aria-hidden />
            )}
            Start research
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
