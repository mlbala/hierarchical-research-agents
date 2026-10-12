/**
 * Light/dark theme with a system default.
 *
 * A tiny shared store (useSyncExternalStore) keeps every consumer in sync,
 * e.g. the sidebar toggle and the toast renderer. The explicit choice is kept
 * in localStorage; "system" follows the OS preference live.
 */
import { useCallback, useSyncExternalStore } from 'react'

export type Theme = 'light' | 'dark' | 'system'
const STORAGE_KEY = 'research-hive-theme'
const listeners = new Set<() => void>()

function readStored(): Theme {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

let current: Theme = typeof window === 'undefined' ? 'system' : readStored()

function prefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function resolve(theme: Theme): 'light' | 'dark' {
  return theme === 'system' ? (prefersDark() ? 'dark' : 'light') : theme
}

/** Apply the current theme to <html>; called once at startup and on change. */
export function applyTheme(): void {
  document.documentElement.classList.toggle('dark', resolve(current) === 'dark')
}

function emit(): void {
  applyTheme()
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  const onSystemChange = () => current === 'system' && emit()
  media.addEventListener('change', onSystemChange)
  return () => {
    listeners.delete(listener)
    media.removeEventListener('change', onSystemChange)
  }
}

export function useTheme() {
  const theme = useSyncExternalStore(subscribe, () => current)
  const setTheme = useCallback((next: Theme) => {
    try {
      if (next === 'system') window.localStorage.removeItem(STORAGE_KEY)
      else window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Storage can be unavailable (private browsing); the theme still applies.
    }
    current = next
    emit()
  }, [])
  return { theme, resolved: resolve(theme), setTheme }
}
