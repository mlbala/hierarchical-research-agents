import type { SupportLevel } from './types'

/** Colour treatment for evidence support levels. */
export const SUPPORT_STYLE: Record<SupportLevel, string> = {
  supported: 'border-success/30 bg-success/10 text-success',
  tentative: 'border-warning/30 bg-warning/10 text-warning',
  conflicting: 'border-destructive/30 bg-destructive/10 text-destructive',
}
