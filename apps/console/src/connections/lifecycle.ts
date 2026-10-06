import type { RuntimeProfileState } from './runtime'
import type { FleetControlFreshness } from './controlState'
import type { MessageKey } from '../i18n/catalogs'

export type ConnectionLifecycle =
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'stale'
  | 'offline'
  | 'unpaired'
  | 'revoked'
  | 'expired'
  | 'attention'
  | 'stored'

type ConnectionObservation = {
  control?: unknown
  error?: string
  freshness: FleetControlFreshness
}

export function connectionLifecycle(
  state: RuntimeProfileState | undefined,
  observation: ConnectionObservation | undefined,
): ConnectionLifecycle {
  if (!state) return 'stored'
  if (state.status === 'restoring') return observation?.control ? 'reconnecting' : 'connecting'
  if (state.status === 'connected') {
    if (observation?.freshness === 'stale') return 'stale'
    return 'live'
  }
  if (state.status === 'error') {
    if (!state.retryable) return 'attention'
    return observation?.control ? 'stale' : 'offline'
  }
  return state.status
}

export function pairingErrorMessageKey(cause: unknown): MessageKey {
  const message = (cause instanceof Error ? cause.message : String(cause ?? '')).toLowerCase()
  if (message.includes('unsupported_pairing_version')) return 'connections.pairingUnsupported'
  if (
    message.includes('invalid_pairing_link')
    || message.includes('invalid_pairing')
  ) return 'connections.pairingInvalid'
  if (message.includes('duplicate_origin')) return 'connections.pairingDuplicate'
  return 'connections.pairingFailed'
}
