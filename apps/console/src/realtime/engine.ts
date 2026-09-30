import { ConsoleHttpError, type ConsoleClient } from '../api/client'
import {
  applyRealtimeFrame,
  beginConnecting,
  boundedBackoffDelay,
  createRealtimeState,
  decodeRealtimeFrame,
  markOffline,
  markStale,
  replaceSnapshot,
  socketOpened,
  socketReconnecting,
  type RealtimeFrame,
  type RealtimeState,
} from './state'

export type RealtimeClient = Pick<ConsoleClient, 'snapshot' | 'webSocketTicket'>
export type RealtimeSocket = {
  onopen: (() => void) | null
  onmessage: ((event: { data: string }) => void) | null
  onclose: ((event: { code: number; reason: string; wasClean: boolean }) => void) | null
  onerror: (() => void) | null
  close: (code?: number, reason?: string) => void
}
export type RealtimeSocketFactory = (url: string) => RealtimeSocket
export type RealtimeScheduler = {
  setTimeout: (callback: () => void, delayMs: number) => unknown
  clearTimeout: (handle: unknown) => void
}
export type RealtimeEngineOptions = {
  socketFactory?: RealtimeSocketFactory
  scheduler?: RealtimeScheduler
  reconnectBaseMs?: number
  reconnectMaxMs?: number
  reconnectJitter?: (delayMs: number, attempt: number) => number
}
type Listener = (state: RealtimeState) => void

const browserScheduler: RealtimeScheduler = {
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
}
function browserSocketFactory(url: string): RealtimeSocket {
  return new WebSocket(url) as unknown as RealtimeSocket
}
export function consoleEventsUrl(origin: string, ticket: string, cursor: number): string {
  const url = new URL('/console/events', new URL(origin).origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('ticket', ticket)
  url.searchParams.set('since', String(cursor))
  return url.toString()
}
function errorCode(error: unknown): string {
  if (error instanceof ConsoleHttpError) return error.code
  if (error instanceof Error && error.message) return error.message
  return 'realtime_error'
}

export class RealtimeConsoleEngine {
  private state = createRealtimeState()
  private readonly listeners = new Set<Listener>()
  private readonly socketFactory: RealtimeSocketFactory
  private readonly scheduler: RealtimeScheduler
  private readonly reconnectBaseMs: number
  private readonly reconnectMaxMs: number
  private readonly reconnectJitter: (delayMs: number, attempt: number) => number
  private socket: RealtimeSocket | null = null
  private reconnectTimer: unknown = null
  private refreshTimer: unknown = null
  private running = false
  private generation = 0
  private refreshInFlight = false
  private refreshAgain = false
  private resyncInFlight = false

  constructor(private readonly client: RealtimeClient, private readonly origin: string, options: RealtimeEngineOptions = {}) {
    this.origin = new URL(origin).origin
    this.socketFactory = options.socketFactory ?? browserSocketFactory
    this.scheduler = options.scheduler ?? browserScheduler
    this.reconnectBaseMs = options.reconnectBaseMs ?? 250
    this.reconnectMaxMs = options.reconnectMaxMs ?? 8000
    this.reconnectJitter = options.reconnectJitter ?? ((delayMs) => delayMs)
  }

  getState(): RealtimeState { return this.state }
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  async start(): Promise<void> {
    if (this.running) return
    this.running = true
    this.setState(beginConnecting(this.state))
    try {
      const snapshot = await this.client.snapshot()
      if (!this.running) return
      this.setState(replaceSnapshot(this.state, snapshot, true))
      await this.connect(false)
    } catch (error) {
      if (this.running) this.handleConnectionFailure(error)
    }
  }

  stop(): void {
    if (!this.running && this.state.status === 'offline') return
    this.running = false
    this.generation += 1
    this.clearReconnectTimer()
    this.clearRefreshTimer()
    const socket = this.socket
    this.socket = null
    if (socket) {
      socket.onopen = null
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      socket.close(1000, 'console_stopped')
    }
    this.setState(markOffline(this.state))
  }

  async retryNow(): Promise<void> {
    if (!this.running) return this.start()
    this.clearReconnectTimer()
    this.clearRefreshTimer()
    if (this.state.snapshot === null || this.state.staleReason === 'journal_gap') {
      return this.resync(this.state.staleReason ?? 'manual_retry')
    }
    this.generation += 1
    const socket = this.socket
    this.socket = null
    if (socket) {
      socket.onopen = null
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      socket.close(1000, 'console_foreground_recover')
    }
    return this.connect(true)
  }

  private setState(next: RealtimeState): void {
    this.state = next
    for (const listener of this.listeners) listener(next)
  }

  private async connect(reconnecting: boolean): Promise<void> {
    if (!this.running || this.state.snapshot === null) return
    const generation = ++this.generation
    this.clearReconnectTimer()
    this.setState(beginConnecting(this.state, reconnecting))
    try {
      const ticket = await this.client.webSocketTicket()
      if (!this.running || generation !== this.generation) return
      const socket = this.socketFactory(consoleEventsUrl(this.origin, ticket.ticket, this.state.cursor))
      this.socket = socket
      socket.onopen = () => {
        if (this.running && generation === this.generation) this.setState(socketOpened(this.state))
      }
      socket.onmessage = (event) => {
        if (this.running && generation === this.generation) this.onMessage(event.data)
      }
      socket.onclose = (event) => {
        if (!this.running || generation !== this.generation) return
        this.socket = null
        if (event.code === 4409) return void this.resync('journal_gap')
        if (event.code === 4400) return void this.resync('invalid_cursor')
        if (event.code === 4401 || event.code === 4403) {
          this.setState(markOffline(this.state, event.reason || `websocket_${event.code}`))
          return
        }
        this.scheduleReconnect(event.reason || `websocket_${event.code}`)
      }
      socket.onerror = () => {}
    } catch (error) {
      if (this.running && generation === this.generation) this.handleConnectionFailure(error)
    }
  }

  private onMessage(data: string): void {
    let frame: RealtimeFrame
    try {
      frame = decodeRealtimeFrame(JSON.parse(data) as unknown)
    } catch (error) {
      void this.resync(`invalid_frame:${errorCode(error)}`)
      return
    }
    this.setState(applyRealtimeFrame(this.state, frame))
    if (frame.type === 'resync_required') return void this.resync(frame.reason)
    if (frame.type === 'heartbeat') {
      if (this.state.staleReason === 'heartbeat_cursor_ahead') void this.resync('heartbeat_cursor_ahead')
      return
    }
    if (this.state.staleReason === 'cursor_gap') return void this.resync('cursor_gap')
    this.queueSnapshotRefresh()
  }

  private queueSnapshotRefresh(): void {
    if (!this.running) return
    if (this.refreshInFlight) {
      this.refreshAgain = true
      return
    }
    void this.refreshSnapshot()
  }

  private async refreshSnapshot(): Promise<void> {
    if (!this.running || this.refreshInFlight) return
    this.refreshInFlight = true
    try {
      do {
        this.refreshAgain = false
        const snapshot = await this.client.snapshot()
        if (!this.running) return
        this.setState(replaceSnapshot(this.state, snapshot, false))
        if (snapshot.highWaterSeq < this.state.cursor) this.refreshAgain = true
      } while (this.running && this.refreshAgain)
    } catch (error) {
      if (this.running) {
        this.setState(markStale(this.state, 'snapshot_refresh_failed', errorCode(error)))
        this.scheduleSnapshotRefresh()
      }
    } finally {
      this.refreshInFlight = false
    }
  }

  private async resync(reason: string): Promise<void> {
    if (!this.running || this.resyncInFlight) return
    this.resyncInFlight = true
    this.generation += 1
    const socket = this.socket
    this.socket = null
    if (socket) {
      socket.onopen = null
      socket.onmessage = null
      socket.onclose = null
      socket.onerror = null
      socket.close(1000, 'console_resync')
    }
    this.setState(markStale(this.state, reason))
    try {
      const snapshot = await this.client.snapshot()
      if (!this.running) return
      this.setState(replaceSnapshot(this.state, snapshot, true))
      this.resyncInFlight = false
      await this.connect(true)
    } catch (error) {
      if (!this.running) return
      this.resyncInFlight = false
      this.setState(markStale(this.state, reason, errorCode(error)))
      this.scheduleResync(reason)
    }
  }

  private handleConnectionFailure(error: unknown): void {
    if (error instanceof ConsoleHttpError && !error.retryable) {
      this.setState(markOffline(this.state, error.code))
      return
    }
    this.scheduleReconnect(errorCode(error))
  }
  private scheduleReconnect(error?: string): void {
    if (!this.running || this.reconnectTimer !== null) return
    const attempt = this.state.reconnectAttempt + 1
    const delay = this.backoffDelay(attempt)
    this.setState(socketReconnecting(this.state, attempt, error))
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null
      if (this.state.snapshot === null) void this.resync('initial_snapshot_retry')
      else void this.connect(true)
    }, delay)
  }
  private scheduleResync(reason: string): void {
    if (!this.running || this.reconnectTimer !== null) return
    const attempt = this.state.reconnectAttempt + 1
    const delay = this.backoffDelay(attempt)
    this.setState(socketReconnecting(this.state, attempt, this.state.lastError))
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null
      void this.resync(reason)
    }, delay)
  }
  private scheduleSnapshotRefresh(): void {
    if (!this.running || this.refreshTimer !== null) return
    const delay = this.backoffDelay(Math.max(1, this.state.reconnectAttempt + 1))
    this.refreshTimer = this.scheduler.setTimeout(() => {
      this.refreshTimer = null
      this.queueSnapshotRefresh()
    }, delay)
  }
  private backoffDelay(attempt: number): number {
    const bounded = boundedBackoffDelay(
      Math.max(0, attempt - 1),
      this.reconnectBaseMs,
      this.reconnectMaxMs,
    )
    const jittered = this.reconnectJitter(bounded, attempt)
    if (!Number.isFinite(jittered)) return bounded
    return Math.max(0, Math.min(this.reconnectMaxMs, Math.round(jittered)))
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === null) return
    this.scheduler.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
  }
  private clearRefreshTimer(): void {
    if (this.refreshTimer === null) return
    this.scheduler.clearTimeout(this.refreshTimer)
    this.refreshTimer = null
  }
}
