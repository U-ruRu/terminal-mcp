import type { ServerVisualState } from './serverPresentation'

export function StatusBadge({
  state,
  label,
}: {
  state: ServerVisualState
  label: string
}) {
  return <span className={'status server-status server-status-' + state}>{label}</span>
}
