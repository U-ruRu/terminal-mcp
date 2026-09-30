import type { RealtimeScheduler } from '../realtime/engine'
import type { FleetAdaptiveReadRuntime, FleetIngressEndpoint } from './adaptiveRuntime'
import type { FleetLifecycle } from './policy'

const browserScheduler: RealtimeScheduler = {
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
}

export class FleetAdaptiveLifecycle implements FleetLifecycle {
  private running = false
  private timer: unknown = null
  private ticks = 0

  constructor(
    private readonly runtime: FleetAdaptiveReadRuntime,
    private readonly endpoints: () => FleetIngressEndpoint[],
    private readonly onStarted?: (active: boolean) => void,
    private readonly onError?: (error: unknown) => void,
    private readonly scheduler: RealtimeScheduler = browserScheduler,
    private readonly syncEveryMs = 1500,
    private readonly evaluateEveryTicks = 10,
  ) {}

  async startAll(): Promise<void> {
    if (this.running) return
    this.running = true
    this.runtime.setBackground(false)
    try {
      const current = this.runtime.getState()
      const active = current
        ? current.status !== 'fallback' && current.status !== 'dormant'
        : await this.runtime.start(this.endpoints())
      this.onStarted?.(active)
      if (active && current) await this.runtime.syncOnce()
    } catch (error) {
      this.onError?.(error)
      this.onStarted?.(false)
    }
    this.schedule()
  }

  async recoverAll(): Promise<void> {
    this.runtime.setBackground(false)
    this.runtime.networkChanged()
    if (!this.running) return this.startAll()
    try {
      await this.runtime.syncOnce()
      await this.runtime.evaluate(this.endpoints())
    } catch (error) {
      this.onError?.(error)
    }
    this.schedule()
  }

  stopAll(): void {
    this.running = false
    this.runtime.setBackground(true)
    if (this.timer !== null) {
      this.scheduler.clearTimeout(this.timer)
      this.timer = null
    }
  }

  private schedule(): void {
    if (!this.running || this.timer !== null) return
    this.timer = this.scheduler.setTimeout(() => {
      this.timer = null
      void this.tick()
    }, this.syncEveryMs)
  }

  private async tick(): Promise<void> {
    if (!this.running) return
    try {
      await this.runtime.syncOnce()
      this.ticks += 1
      if (this.ticks % this.evaluateEveryTicks === 0) {
        await this.runtime.evaluate(this.endpoints())
      }
    } catch (error) {
      this.onError?.(error)
    } finally {
      this.schedule()
    }
  }
}
