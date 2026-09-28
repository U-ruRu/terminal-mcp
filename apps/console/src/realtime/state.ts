import type { ConsoleSnapshotReadModel, JsonRecord } from '../api/models'

export type RealtimeStatus = 'connecting' | 'live' | 'reconnecting' | 'offline' | 'stale'

export type InstanceEvent = {
  seq: number
  eventType: string
  entityType: string
  entityId: string
  actorId?: string
  payload: JsonRecord
  createdAt: string
}

export type RealtimeFrame =
  | { type: 'event'; event: InstanceEvent }
  | { type: 'heartbeat'; cursor: number; highWaterSeq: number }
  | { type: 'resync_required'; reason: string; cursor: number; oldestSeq?: number; highWaterSeq: number }

export type RealtimeState = {
  status: RealtimeStatus
  snapshot: ConsoleSnapshotReadModel | null
  cursor: number
  highWaterSeq: number
  socketConnected: boolean
  reconnectAttempt: number
  staleReason?: string
  lastError?: string
  lastEvent?: InstanceEvent
}

export function createRealtimeState(): RealtimeState {
  return { status: 'offline', snapshot: null, cursor: 0, highWaterSeq: 0, socketConnected: false, reconnectAttempt: 0 }
}

export function beginConnecting(state: RealtimeState, reconnecting = false): RealtimeState {
  return { ...state, status: reconnecting ? 'reconnecting' : 'connecting', socketConnected: false, staleReason: undefined, lastError: undefined }
}

export function replaceSnapshot(state: RealtimeState, snapshot: ConsoleSnapshotReadModel, resetCursor: boolean): RealtimeState {
  const cursor = resetCursor ? snapshot.replayFromSeq : state.cursor
  const caughtUp = resetCursor || snapshot.highWaterSeq >= cursor
  return {
    ...state,
    snapshot,
    cursor,
    highWaterSeq: Math.max(state.highWaterSeq, snapshot.highWaterSeq),
    status: caughtUp ? (state.socketConnected ? 'live' : state.status) : 'stale',
    staleReason: caughtUp ? undefined : 'events_after_snapshot',
    lastError: undefined,
  }
}

export function socketOpened(state: RealtimeState): RealtimeState {
  const snapshotCaughtUp = state.snapshot !== null && state.snapshot.highWaterSeq >= state.cursor
  return {
    ...state,
    status: snapshotCaughtUp ? 'live' : 'stale',
    socketConnected: true,
    reconnectAttempt: 0,
    staleReason: snapshotCaughtUp ? undefined : state.staleReason ?? 'snapshot_behind_cursor',
    lastError: undefined,
  }
}

export function socketReconnecting(state: RealtimeState, attempt: number, error?: string): RealtimeState {
  return { ...state, status: 'reconnecting', socketConnected: false, reconnectAttempt: attempt, lastError: error }
}

export function markOffline(state: RealtimeState, error?: string): RealtimeState {
  return { ...state, status: 'offline', socketConnected: false, lastError: error }
}

export function markStale(state: RealtimeState, reason: string, error?: string): RealtimeState {
  return { ...state, status: 'stale', staleReason: reason, lastError: error }
}

export function applyRealtimeFrame(state: RealtimeState, frame: RealtimeFrame): RealtimeState {
  if (frame.type === 'resync_required') {
    return { ...state, status: 'stale', highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq), staleReason: frame.reason || 'resync_required' }
  }
  if (frame.type === 'heartbeat') {
    if (frame.cursor > state.cursor) {
      return { ...state, status: 'stale', highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq), staleReason: 'heartbeat_cursor_ahead' }
    }
    return { ...state, highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq) }
  }
  const { event } = frame
  if (event.seq <= state.cursor) return { ...state, highWaterSeq: Math.max(state.highWaterSeq, event.seq) }
  if (event.seq !== state.cursor + 1) {
    return { ...state, status: 'stale', highWaterSeq: Math.max(state.highWaterSeq, event.seq), staleReason: 'cursor_gap', lastEvent: event }
  }
  return { ...state, status: 'stale', cursor: event.seq, highWaterSeq: Math.max(state.highWaterSeq, event.seq), staleReason: 'event_pending_refresh', lastEvent: event }
}

export function boundedBackoffDelay(attempt: number, baseMs = 250, maxMs = 8000): number {
  return Math.min(maxMs, baseMs * 2 ** Math.max(0, Math.floor(attempt)))
}

function record(value: unknown, path: string): JsonRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`Invalid realtime frame at ${path}: expected object`)
  return value as JsonRecord
}
function text(value: unknown, path: string): string {
  if (typeof value !== 'string') throw new Error(`Invalid realtime frame at ${path}: expected string`)
  return value
}
function integer(value: unknown, path: string): number {
  if (!Number.isInteger(value) || (value as number) < 0) throw new Error(`Invalid realtime frame at ${path}: expected non-negative integer`)
  return value as number
}
function optionalInteger(value: unknown, path: string): number | undefined {
  if (value === undefined || value === null) return undefined
  return integer(value, path)
}

export function decodeRealtimeFrame(value: unknown): RealtimeFrame {
  const root = record(value, '$')
  const type = text(root.type, '$.type')
  if (type === 'event') {
    const raw = record(root.event, '$.event')
    const actor = raw.actor_id
    if (actor !== undefined && actor !== null && typeof actor !== 'string') throw new Error('Invalid realtime frame at $.event.actor_id: expected string or null')
    return {
      type: 'event',
      event: {
        seq: integer(raw.seq, '$.event.seq'),
        eventType: text(raw.event_type, '$.event.event_type'),
        entityType: text(raw.entity_type, '$.event.entity_type'),
        entityId: text(raw.entity_id, '$.event.entity_id'),
        actorId: typeof actor === 'string' ? actor : undefined,
        payload: record(raw.payload ?? {}, '$.event.payload'),
        createdAt: text(raw.created_at, '$.event.created_at'),
      },
    }
  }
  if (type === 'heartbeat') return { type: 'heartbeat', cursor: integer(root.cursor, '$.cursor'), highWaterSeq: integer(root.high_water_seq, '$.high_water_seq') }
  if (type === 'resync_required') {
    return { type: 'resync_required', reason: text(root.reason, '$.reason'), cursor: integer(root.cursor, '$.cursor'), oldestSeq: optionalInteger(root.oldest_seq, '$.oldest_seq'), highWaterSeq: integer(root.high_water_seq, '$.high_water_seq') }
  }
  throw new Error(`Invalid realtime frame at $.type: unsupported type ${type}`)
}
