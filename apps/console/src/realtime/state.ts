import type { ConsoleSnapshotReadModel, JsonRecord } from '../api/models'

export type RealtimeStatus = 'connecting' | 'live' | 'reconnecting' | 'offline' | 'stale'
export type RealtimeFreshness = 'fresh' | 'catching_up' | 'stale'

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
  freshness: RealtimeFreshness
  catchingUpScopes: string[]
  staleReason?: string
  lastError?: string
  statusSince?: string
  freshnessSince?: string
  lastEvent?: InstanceEvent
}

function transitionTime(value?: string): string { return value ?? new Date().toISOString() }
function since<T>(previous: T, next: T, previousSince: string | undefined, at: string): string {
  return previous === next && previousSince ? previousSince : at
}

export function createRealtimeState(at = transitionTime()): RealtimeState {
  return {
    status: 'connecting', snapshot: null, cursor: 0, highWaterSeq: 0, socketConnected: false,
    reconnectAttempt: 0, freshness: 'stale', catchingUpScopes: [], statusSince: at, freshnessSince: at,
  }
}

export function beginConnecting(state: RealtimeState, reconnecting = false, at = transitionTime()): RealtimeState {
  const status = reconnecting ? 'reconnecting' : 'connecting'
  return {
    ...state, status, statusSince: since(state.status, status, state.statusSince, at),
    socketConnected: false, staleReason: undefined, lastError: undefined,
  }
}

export function replaceSnapshot(state: RealtimeState, snapshot: ConsoleSnapshotReadModel, resetCursor: boolean, at = transitionTime()): RealtimeState {
  const cursor = resetCursor ? snapshot.replayFromSeq : state.cursor
  const caughtUp = resetCursor || snapshot.highWaterSeq >= cursor
  const hazardous = state.status === 'stale' && state.freshness === 'stale' && Boolean(state.staleReason)
  const status = caughtUp
    ? (state.socketConnected ? 'live' : state.status)
    : (state.socketConnected && !hazardous ? 'live' : state.status)
  const freshness = caughtUp ? 'fresh' : (hazardous ? 'stale' : 'catching_up')
  return {
    ...state, snapshot, cursor,
    highWaterSeq: Math.max(state.highWaterSeq, snapshot.highWaterSeq),
    status, statusSince: since(state.status, status, state.statusSince, at),
    freshness, freshnessSince: since(state.freshness, freshness, state.freshnessSince, at),
    catchingUpScopes: caughtUp ? [] : (state.catchingUpScopes.length > 0 ? state.catchingUpScopes : ['*']),
    staleReason: caughtUp ? undefined : (hazardous ? state.staleReason : undefined),
    lastError: undefined,
  }
}

export function socketOpened(state: RealtimeState, at = transitionTime()): RealtimeState {
  const snapshotCaughtUp = state.snapshot !== null && state.snapshot.highWaterSeq >= state.cursor
  const hazardous = state.status === 'stale' && state.freshness === 'stale' && Boolean(state.staleReason)
  const status = hazardous ? 'stale' : 'live'
  const freshness = hazardous ? 'stale' : (snapshotCaughtUp ? 'fresh' : 'catching_up')
  return {
    ...state, status, statusSince: since(state.status, status, state.statusSince, at),
    socketConnected: true, reconnectAttempt: 0,
    freshness, freshnessSince: since(state.freshness, freshness, state.freshnessSince, at),
    catchingUpScopes: snapshotCaughtUp ? [] : (state.catchingUpScopes.length > 0 ? state.catchingUpScopes : ['*']),
    staleReason: hazardous ? state.staleReason : undefined, lastError: undefined,
  }
}

export function socketReconnecting(state: RealtimeState, attempt: number, error?: string, at = transitionTime()): RealtimeState {
  const status = 'reconnecting' as const
  return { ...state, status, statusSince: since(state.status, status, state.statusSince, at), socketConnected: false, reconnectAttempt: attempt, lastError: error }
}

export function markOffline(state: RealtimeState, error?: string, at = transitionTime()): RealtimeState {
  const status = 'offline' as const
  return { ...state, status, statusSince: since(state.status, status, state.statusSince, at), socketConnected: false, lastError: error }
}

export function markStale(state: RealtimeState, reason: string, error?: string, at = transitionTime()): RealtimeState {
  const status = 'stale' as const
  const freshness = 'stale' as const
  return { ...state, status, statusSince: since(state.status, status, state.statusSince, at), freshness, freshnessSince: since(state.freshness, freshness, state.freshnessSince, at), catchingUpScopes: [], staleReason: reason, lastError: error }
}

function addScope(scopes: string[], scope: string): string[] {
  return scopes.includes(scope) ? scopes : [...scopes, scope]
}

export function applyRealtimeFrame(state: RealtimeState, frame: RealtimeFrame, at = transitionTime()): RealtimeState {
  if (frame.type === 'resync_required') {
    return markStale(
      { ...state, highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq) },
      frame.reason || 'resync_required',
      undefined,
      at,
    )
  }
  if (frame.type === 'heartbeat') {
    if (frame.cursor > state.cursor) {
      return markStale(
        { ...state, highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq) },
        'heartbeat_cursor_ahead',
        undefined,
        at,
      )
    }
    return { ...state, highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq) }
  }
  const { event } = frame
  if (event.seq <= state.cursor) return { ...state, highWaterSeq: Math.max(state.highWaterSeq, event.seq) }
  if (event.seq !== state.cursor + 1) {
    return markStale(
      { ...state, highWaterSeq: Math.max(state.highWaterSeq, event.seq), lastEvent: event },
      'cursor_gap',
      undefined,
      at,
    )
  }
  const status = state.socketConnected ? 'live' : state.status
  const freshness = state.freshness === 'stale' ? 'stale' : 'catching_up'
  return {
    ...state, status, statusSince: since(state.status, status, state.statusSince, at),
    cursor: event.seq, highWaterSeq: Math.max(state.highWaterSeq, event.seq),
    freshness, freshnessSince: since(state.freshness, freshness, state.freshnessSince, at),
    catchingUpScopes: state.freshness === 'stale' ? state.catchingUpScopes : addScope(state.catchingUpScopes, event.entityType),
    staleReason: state.freshness === 'stale' ? state.staleReason : undefined, lastEvent: event,
  }
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
