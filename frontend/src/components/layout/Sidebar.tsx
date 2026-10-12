import { Folder, Hexagon, Monitor, Moon, Search, Sun } from 'lucide-react'
import { NavLink } from 'react-router'
import { cn } from 'cn'

import { ModeBadge, StatusDot } from '@/components/common/status'
import { NewProjectDialog } from '@/components/layout/NewProjectDialog'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { useConfig, useProjects, useRecentRuns } from '@/hooks/queries'
import { useTheme, type Theme } from '@/hooks/use-theme'

const navItem = ({ isActive }: { isActive: boolean }) =>
  cn(
    'flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm transition-colors',
    'hover:bg-background/70 focus-visible:outline-2 focus-visible:outline-offset-1',
    isActive ? 'bg-background font-medium shadow-xs' : 'text-sidebar-foreground/90',
  )

const NEXT_THEME: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' }

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const config = useConfig()
  const projects = useProjects()
  const recent = useRecentRuns()
  const { theme, setTheme } = useTheme()
  const ThemeIcon = theme === 'dark' ? Moon : theme === 'light' ? Sun : Monitor

  return (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex items-center gap-2 px-4 pt-5 pb-3">
        <NavLink to="/" onClick={onNavigate} className="flex items-center gap-2 rounded-md">
          <Hexagon aria-hidden className="size-6 fill-primary/15 text-primary" />
          <span className="text-base font-semibold tracking-tight">Research Hive</span>
        </NavLink>
      </div>
      <div className="px-4 pb-3">
        {config.data ? (
          <ModeBadge mode={config.data.execution_mode} ready={config.data.live_ready} />
        ) : (
          <Skeleton className="h-5 w-24" />
        )}
      </div>

      <div className="space-y-1 px-3">
        <NavLink to="/search" className={navItem} onClick={onNavigate}>
          <Search aria-hidden className="size-4" /> Search reports
        </NavLink>
      </div>

      <ScrollArea className="mt-3 min-h-0 flex-1 px-3">
        <nav aria-label="Projects" className="space-y-1 pb-4">
          <div className="flex items-center justify-between px-2.5 pt-1 pb-1.5">
            <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Projects
            </h2>
          </div>
          {projects.isLoading &&
            Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="mx-2.5 h-6" />)}
          {projects.data?.items.length === 0 && (
            <p className="px-2.5 text-sm text-muted-foreground">No projects yet.</p>
          )}
          {projects.data?.items.map((project) => (
            <NavLink
              key={project.id}
              to={`/projects/${project.id}`}
              className={navItem}
              onClick={onNavigate}
            >
              <Folder aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="truncate">{project.name}</span>
              <span className="ml-auto text-xs text-muted-foreground tabular-nums">
                {project.run_count}
              </span>
            </NavLink>
          ))}
          <div className="px-1 pt-2">
            <NewProjectDialog />
          </div>
        </nav>

        <Separator className="my-2" />

        <nav aria-label="Recent research" className="space-y-1 pb-6">
          <h2 className="px-2.5 pt-2 pb-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            Recent research
          </h2>
          {recent.data?.items.length === 0 && (
            <p className="px-2.5 text-sm text-muted-foreground">Nothing yet.</p>
          )}
          {recent.data?.items.map((run) => (
            <NavLink key={run.id} to={`/runs/${run.id}`} className={navItem} onClick={onNavigate}>
              <StatusDot status={run.status} />
              <span className="line-clamp-2 text-left">{run.question}</span>
            </NavLink>
          ))}
        </nav>
      </ScrollArea>

      <div className="flex items-center justify-between border-t px-4 py-3 text-xs text-muted-foreground">
        <span>Shared workspace · no login</span>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setTheme(NEXT_THEME[theme])}
          aria-label={`Theme: ${theme}. Switch to ${NEXT_THEME[theme]}.`}
          title={`Theme: ${theme}`}
        >
          <ThemeIcon aria-hidden />
        </Button>
      </div>
    </div>
  )
}
