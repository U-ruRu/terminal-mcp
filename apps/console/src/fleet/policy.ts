import type { FetchLike } from '../api/client'
import type { RealtimeScheduler } from '../realtime/engine'

export const DEFAULT_FLEET_MAX_CONCURRENT_STARTS = 4
export const DEFAULT_FLEET_REQUEST_TIMEOUT_MS = 10_000
export const DEFAULT_FLEET_BACKGROUND_SUSPEND_MS = 30_000

const browserScheduler: RealtimeScheduler = {
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
}

export function withRequestTimeout(
  fetcher: FetchLike,
  timeoutMs = DEFAULT_FLEET_REQUEST_TIMEOUT_MS,
  scheduler: RealtimeScheduler = browserScheduler,
): FetchLike {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error('request_timeout_must_be_positive')
  }

  return async (input, init = {}) => {
    const controller = new AbortController()
    const upstream = init.signal
    const abortFromUpstream = () => controller.abort(upstream?.reason)

    if (upstream?.aborted) abortFromUpstream()
    else upstream?.addEventListener('abort', abortFromUpstream, { once: true })

    let timer: unknown = null
    try {
      return await new Promise<Response>((resolve, reject) => {
        let settled = false
        const finish = (callback: () => void) => {
          if (settled) return
          settled = true
          callback()
        }

        timer = scheduler.setTimeout(() => {
          controller.abort('request_timeout')
          finish(() => reject(new TypeError('request_timeout')))
        }, timeoutMs)

        void fetcher(input, { ...init, signal: controller.signal }).then(
          (response) => finish(() => resolve(response)),
          (error) => finish(() => reject(error)),
        )
      })
    } finally {
      if (timer !== null) scheduler.clearTimeout(timer)
      upstream?.removeEventListener('abort', abortFromUpstream)
    }
  }
}

export type FleetVisibilitySource = {
  visibilityState: string
  addEventListener(type: 'visibilitychange', listener: () => void): void
  removeEventListener(type: 'visibilitychange', listener: () => void): void
}

export type FleetLifecycle = {
  startAll(): Promise<void>
  recoverAll(): Promise<void>
  stopAll(): void
}

export type FleetVisibilityEvent =
  | 'app_background'
  | 'app_foreground'
  | 'background_suspended'
  | 'foreground_recover_requested'
  | 'foreground_restart_requested'

export class FleetVisibilityController {
  private timer: unknown = null
  private suspended = false
  private started = false
  private wasHidden = false

  constructor(
    private readonly fleet: FleetLifecycle,
    private readonly visibility: FleetVisibilitySource,
    private readonly scheduler: RealtimeScheduler = browserScheduler,
    private readonly suspendAfterMs = DEFAULT_FLEET_BACKGROUND_SUSPEND_MS,
    private readonly onLifecycleEvent?: (event: FleetVisibilityEvent) => void,
  ) {
    if (!Number.isFinite(suspendAfterMs) || suspendAfterMs < 0) {
      throw new Error('background_suspend_must_be_non_negative')
    }
  }

  start(): void {
    if (this.started) return
    this.started = true
    this.visibility.addEventListener('visibilitychange', this.onVisibilityChange)
    this.onVisibilityChange()
  }

  stop(): void {
    if (!this.started) return
    this.started = false
    this.visibility.removeEventListener('visibilitychange', this.onVisibilityChange)
    this.clearTimer()
  }

  private readonly onVisibilityChange = (): void => {
    if (!this.started) return
    if (this.visibility.visibilityState === 'hidden') {
      if (!this.wasHidden) this.onLifecycleEvent?.('app_background')
      this.wasHidden = true
      this.scheduleSuspend()
      return
    }

    this.clearTimer()
    if (!this.wasHidden) return
    this.wasHidden = false
    this.onLifecycleEvent?.('app_foreground')
    if (this.suspended) {
      this.suspended = false
      this.onLifecycleEvent?.('foreground_restart_requested')
      void this.fleet.startAll().catch(() => {})
      return
    }
    this.onLifecycleEvent?.('foreground_recover_requested')
    void this.fleet.recoverAll().catch(() => {})
  }

  private scheduleSuspend(): void {
    if (this.suspended || this.timer !== null) return
    this.timer = this.scheduler.setTimeout(() => {
      this.timer = null
      if (!this.started || this.visibility.visibilityState !== 'hidden') return
      this.suspended = true
      this.onLifecycleEvent?.('background_suspended')
      this.fleet.stopAll()
    }, this.suspendAfterMs)
  }

  private clearTimer(): void {
    if (this.timer === null) return
    this.scheduler.clearTimeout(this.timer)
    this.timer = null
  }
}
