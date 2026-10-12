import { FolderOpen, FolderPlus, Hexagon, Network, Quote, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState, ErrorState } from '@/components/common/status'
import { NewProjectDialog } from '@/components/layout/NewProjectDialog'
import { RunList } from '@/components/run/RunList'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useProjects, useRecentRuns } from '@/hooks/queries'
import { errorMessage } from '@/lib/api'
import { formatRelative } from '@/lib/format'

const FEATURES = [
  {
    icon: Network,
    title: 'Seven bounded agents',
    text: 'A director, two team leads and four specialists with fixed roles and limits.',
  },
  {
    icon: Quote,
    title: 'Evidence you can check',
    text: 'Every finding keeps a verbatim excerpt; every claim cites a numbered source.',
  },
  {
    icon: ShieldCheck,
    title: 'Guardrails in code',
    text: 'Safe page retrieval, budgets, cancellation and citation validation on the server.',
  },
]

export function HomePage() {
  const projects = useProjects()
  const recent = useRecentRuns()

  return (
    <div className="mx-auto max-w-5xl space-y-10">
      <section className="space-y-4">
        <div className="flex items-center gap-3">
          <Hexagon aria-hidden className="size-9 fill-primary/15 text-primary" />
          <h1 className="text-3xl font-semibold tracking-tight">Research Hive</h1>
        </div>
        <p className="max-w-2xl text-lg text-muted-foreground">
          Ask a question; a small team of AI agents plans the research, gathers real sources,
          reviews the evidence and writes a readable, cited report.
        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, text }) => (
            <div key={title} className="rounded-xl border bg-card p-4 shadow-xs">
              <Icon aria-hidden className="mb-2 size-5 text-primary" />
              <p className="font-medium">{title}</p>
              <p className="text-sm text-muted-foreground">{text}</p>
            </div>
          ))}
        </div>
      </section>

      <section aria-labelledby="projects-heading" className="space-y-4">
        <div className="flex items-center justify-between gap-2">
          <h2 id="projects-heading" className="text-lg font-semibold">
            Projects
          </h2>
          <NewProjectDialog
            trigger={
              <Button size="sm">
                <FolderPlus aria-hidden /> New project
              </Button>
            }
          />
        </div>
        {projects.isLoading && (
          <div className="grid gap-3 sm:grid-cols-2">
            <Skeleton className="h-24" />
            <Skeleton className="h-24" />
          </div>
        )}
        {projects.isError && (
          <ErrorState message={errorMessage(projects.error)} onRetry={() => void projects.refetch()} />
        )}
        {projects.data?.items.length === 0 && (
          <EmptyState
            icon={FolderOpen}
            title="Create your first project"
            action={<NewProjectDialog trigger={<Button>Create a project</Button>} />}
          >
            A project groups related research questions and their reports.
          </EmptyState>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          {projects.data?.items.map((project) => (
            <Link key={project.id} to={`/projects/${project.id}`} className="group rounded-xl">
              <Card className="h-full shadow-xs transition group-hover:shadow-md group-focus-visible:ring-2 group-focus-visible:ring-ring">
                <CardHeader>
                  <CardTitle className="text-base">{project.name}</CardTitle>
                  <CardDescription className="line-clamp-2">
                    {project.description || 'No description'}
                  </CardDescription>
                  <p className="text-xs text-muted-foreground">
                    {project.run_count} run{project.run_count === 1 ? '' : 's'}
                    {project.last_run_at && ` · last ${formatRelative(project.last_run_at)}`}
                  </p>
                </CardHeader>
              </Card>
            </Link>
          ))}
        </div>
      </section>

      {!!recent.data?.items.length && (
        <section aria-labelledby="recent-heading" className="space-y-4">
          <h2 id="recent-heading" className="text-lg font-semibold">
            Recent research
          </h2>
          <RunList runs={recent.data.items} />
        </section>
      )}
    </div>
  )
}
