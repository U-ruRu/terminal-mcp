import { ConsoleHttpError } from '../api/client'
import type { ManagedFleetControlReadModel } from '../api/models'
import { diagnosticErrorCode } from '../diagnostics/journal'
import { isFleetControlRevisionRegression, type FleetControlFreshness } from './controlState'

export type ControlObservation = {
  control?: ManagedFleetControlReadModel
  error?: string
  elapsedMs?: number
  status?: number
  freshness: FleetControlFreshness
  observedAt?: number
}
export async function observeControl(load: () => Promise<ManagedFleetControlReadModel>): Promise<ControlObservation> {
  const started = Date.now()
  try { return { control: await load(), freshness: 'fresh', observedAt: Date.now() } }
  catch (error) {
    return { freshness: 'unknown',
      error: error instanceof ConsoleHttpError ? diagnosticErrorCode(error.code) ?? 'control_request_failed' : 'control_unavailable',
      elapsedMs: error instanceof ConsoleHttpError ? error.elapsedMs ?? Date.now() - started : Date.now() - started,
      status: error instanceof ConsoleHttpError ? error.status : undefined,
    }
  }
}
export function mergeControlObservation(prior: ControlObservation | undefined, next: ControlObservation): ControlObservation {
  if (next.control && !isFleetControlRevisionRegression(next.control, prior?.control)) return next
  return { ...prior, freshness: prior?.control ? 'stale' : 'unknown',
    error: next.control ? 'control_revision_regression' : next.error, elapsedMs: next.elapsedMs, status: next.status }
}
export function currentControlObservation(value: ControlObservation, now: number): ControlObservation {
  return value.freshness === 'fresh' && (!value.observedAt || now - value.observedAt > 30_000)
    ? { ...value, freshness: value.control ? 'stale' : 'unknown', error: 'control_observation_expired' } : value
}

/** Tracks asynchronous request ownership; its mutable counters are never presentation state. */
export class ControlReadLifetime {
  private epoch = 0
  private active = false
  private pending = 0
  activate(): void { this.active = true; this.epoch++ }
  deactivate(): void { this.active = false; this.epoch++ }
  begin(): number { this.pending++; return ++this.epoch }
  finish(): void { this.pending = Math.max(0, this.pending - 1) }
  isCurrent(epoch: number): boolean { return this.active && this.epoch === epoch }
  idle(): boolean { return this.pending === 0 }
}
