import { History } from 'lucide-react'
import { useParams } from 'react-router'

import { EmptyState, ErrorState } from '@/components/common/status'
import { ResearchForm } from '@/components/research/ResearchForm'
import { RunList } from '@/components/run/RunList'
import { Skeleton } from '@/components/ui/skeleton'
import { useProject, useProjectRuns } from '@/hooks/queries'
import { errorMessage } from '@/lib/api'

export function ProjectPage() {
  const { projectId = '' } = useParams()
  const project = useProject(projectId)
  const runs = useProjectRuns(projectId)

  if (project.isLoading) return <Skeleton className="h-40" />
  if (project.isError || !project.data) {
    return (
      <ErrorState
        title="Project unavailable"
        message={errorMessage(project.error)}
        onRetry={() => void project.refetch()}
      />
    )
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <header className="space-y-1">
        <p className="text-sm text-muted-foreground">Project</p>
        <h1 className="text-2xl font-semibold tracking-tight">{project.data.name}</h1>
        {project.data.description && (
          <p className="max-w-2xl text-muted-foreground">{project.data.description}</p>
        )}
      </header>

      <ResearchForm projectId={projectId} />

      <section aria-labelledby="history-heading" className="space-y-3">
        <h2 id="history-heading" className="text-lg font-semibold">
          Research history
        </h2>
        {runs.isLoading && <Skeleton className="h-24" />}
        {runs.isError && (
          <ErrorState message={errorMessage(runs.error)} onRetry={() => void runs.refetch()} />
        )}
        {runs.data?.items.length === 0 && (
          <EmptyState icon={History} title="No research yet">
            Ask your first question above. Runs and reports are saved here.
          </EmptyState>
        )}
        {!!runs.data?.items.length && <RunList runs={runs.data.items} />}
      </section>
    </div>
  )
}
