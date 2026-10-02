import type { ConsoleSnapshotReadModel, JsonRecord, PersistentMutationResult, PersistentPolicyReadModel, PersistentSlotReadModel, PersistentWorkSessionReadModel } from '../api/models'

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
  lastEvent?: InstanceEvent
}

export function createRealtimeState(): RealtimeState {
  return {
    status: 'connecting', snapshot: null, cursor: 0, highWaterSeq: 0, socketConnected: false,
    reconnectAttempt: 0, freshness: 'stale', catchingUpScopes: [],
  }
}

export function beginConnecting(state: RealtimeState, reconnecting = false): RealtimeState {
  return {
    ...state,
    status: reconnecting ? 'reconnecting' : 'connecting',
    socketConnected: false,
    staleReason: undefined,
    lastError: undefined,
  }
}

export function replaceSnapshot(state: RealtimeState, snapshot: ConsoleSnapshotReadModel, resetCursor: boolean): RealtimeState {
  const cursor = resetCursor ? snapshot.replayFromSeq : state.cursor
  const caughtUp = resetCursor || snapshot.highWaterSeq >= cursor
  const hazardous = state.status === 'stale' && state.freshness === 'stale' && Boolean(state.staleReason)
  return {
    ...state,
    snapshot,
    cursor,
    highWaterSeq: Math.max(state.highWaterSeq, snapshot.highWaterSeq),
    status: caughtUp
      ? (state.socketConnected ? 'live' : state.status)
      : (state.socketConnected && !hazardous ? 'live' : state.status),
    freshness: caughtUp ? 'fresh' : (hazardous ? 'stale' : 'catching_up'),
    catchingUpScopes: caughtUp ? [] : (state.catchingUpScopes.length > 0 ? state.catchingUpScopes : ['*']),
    staleReason: caughtUp ? undefined : (hazardous ? state.staleReason : undefined),
    lastError: undefined,
  }
}

export function socketOpened(state: RealtimeState): RealtimeState {
  const snapshotCaughtUp = state.snapshot !== null && state.snapshot.highWaterSeq >= state.cursor
  const hazardous = state.status === 'stale' && state.freshness === 'stale' && Boolean(state.staleReason)
  return {
    ...state,
    status: hazardous ? 'stale' : 'live',
    socketConnected: true,
    reconnectAttempt: 0,
    freshness: hazardous ? 'stale' : (snapshotCaughtUp ? 'fresh' : 'catching_up'),
    catchingUpScopes: snapshotCaughtUp ? [] : (state.catchingUpScopes.length > 0 ? state.catchingUpScopes : ['*']),
    staleReason: hazardous ? state.staleReason : undefined,
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
  return { ...state, status: 'stale', freshness: 'stale', catchingUpScopes: [], staleReason: reason, lastError: error }
}

function addScope(scopes: string[], scope: string): string[] {
  return scopes.includes(scope) ? scopes : [...scopes, scope]
}

export function applyRealtimeFrame(state: RealtimeState, frame: RealtimeFrame): RealtimeState {
  if (frame.type === 'resync_required') {
    return markStale(
      { ...state, highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq) },
      frame.reason || 'resync_required',
    )
  }
  if (frame.type === 'heartbeat') {
    if (frame.cursor > state.cursor) {
      return markStale(
        { ...state, highWaterSeq: Math.max(state.highWaterSeq, frame.highWaterSeq) },
        'heartbeat_cursor_ahead',
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
    )
  }
  return {
    ...state,
    status: state.socketConnected ? 'live' : state.status,
    cursor: event.seq,
    highWaterSeq: Math.max(state.highWaterSeq, event.seq),
    freshness: state.freshness === 'stale' ? 'stale' : 'catching_up',
    catchingUpScopes: state.freshness === 'stale'
      ? state.catchingUpScopes
      : addScope(state.catchingUpScopes, event.entityType),
    staleReason: state.freshness === 'stale' ? state.staleReason : undefined,
    lastEvent: event,
  }
}

function rawRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}
function rawString(value: unknown): string | undefined { return typeof value === 'string' ? value : undefined }
function rawInteger(value: unknown): number | undefined { return Number.isInteger(value) ? Number(value) : undefined }
function rawBoolean(value: unknown): boolean | undefined { return typeof value === 'boolean' ? value : undefined }

function workSessionPatch(value: unknown): PersistentWorkSessionReadModel | undefined {
  const item = rawRecord(value)
  if (!item) return undefined
  const workSessionId = rawString(item.work_session_id)
  const sessionEpoch = rawInteger(item.session_epoch)
  const authorityNodeId = rawString(item.authority_node_id)
  const authorityEpoch = rawInteger(item.authority_epoch)
  const startedAt = rawString(item.started_at)
  const hardExpiresAt = rawString(item.hard_expires_at)
  const state = rawString(item.state)
  if (!workSessionId || sessionEpoch === undefined || !authorityNodeId || authorityEpoch === undefined || !startedAt || !hardExpiresAt || !state) return undefined
  return {
    workSessionId, sessionEpoch, authorityNodeId, authorityEpoch, startedAt, hardExpiresAt, state,
    originInstanceId: rawString(item.origin_instance_id),
  }
}

function slotPatch(prior: PersistentSlotReadModel | undefined, payload: Record<string, unknown>): PersistentSlotReadModel | undefined {
  const raw = rawRecord(payload.slot)
  if (!raw) return prior
  const logicalAgentId = rawString(raw.logical_agent_id) ?? prior?.logicalAgentId
  const displayName = rawString(raw.display_name) ?? prior?.displayName
  const state = rawString(raw.state) ?? prior?.state
  const authorityNodeId = rawString(raw.authority_node_id) ?? prior?.authorityNodeId
  const authorityEpoch = rawInteger(raw.authority_epoch) ?? prior?.authorityEpoch
  const slotRevision = rawInteger(raw.slot_revision) ?? prior?.slotRevision
  const selectorGeneration = rawInteger(raw.selector_generation) ?? prior?.selectorGeneration
  const authGeneration = rawInteger(raw.auth_generation) ?? prior?.authGeneration
  const createdAt = rawString(raw.created_at) ?? prior?.createdAt
  const updatedAt = rawString(raw.updated_at) ?? prior?.updatedAt
  const selectorRaw = rawRecord(payload.selector)
  const selector = rawString(selectorRaw?.selector) ?? prior?.selector
  const serverNow = rawString(payload.server_now) ?? prior?.serverNow
  if (!logicalAgentId || !displayName || !state || !authorityNodeId || authorityEpoch === undefined || slotRevision === undefined || selectorGeneration === undefined || authGeneration === undefined || !createdAt || !updatedAt || !selector || !serverNow) return prior

  let access = prior?.access
  const accessRaw = rawRecord(payload.access)
  const publicName = rawString(accessRaw?.public_name)
  const accessGeneration = rawInteger(accessRaw?.access_generation)
  const accessStatus = rawString(accessRaw?.status)
  if (publicName && accessGeneration !== undefined && accessStatus) {
    access = { publicName, accessGeneration, status: accessStatus }
  }

  const returnedSession = workSessionPatch(payload.work_session)
  const workSession = state === 'active' || state === 'stopping'
    ? (returnedSession ?? prior?.workSession)
    : undefined

  return {
    logicalAgentId, displayName, state, authorityNodeId, authorityEpoch, slotRevision, selector,
    selectorGeneration, authGeneration, access, createdAt, updatedAt, serverNow, workSession,
    claims: prior?.claims ?? [], audit: prior?.audit ?? [], attachments: prior?.attachments ?? [],
  }
}

function policyPatch(prior: PersistentPolicyReadModel, value: unknown): PersistentPolicyReadModel {
  const raw = rawRecord(value)
  if (!raw) return prior
  return {
    durationSeconds: rawInteger(raw.duration_seconds) ?? prior.durationSeconds,
    warningAfterSeconds: rawInteger(raw.warning_after_seconds) ?? prior.warningAfterSeconds,
    alertAfterSeconds: rawInteger(raw.alert_after_seconds) ?? prior.alertAfterSeconds,
    rearmAfterSeconds: rawInteger(raw.rearm_after_seconds) ?? prior.rearmAfterSeconds,
    manualRearm: rawBoolean(raw.manual_rearm) ?? prior.manualRearm,
    admissionMode: rawString(raw.admission_mode) ?? prior.admissionMode,
    legacyAdmissionEnabled: rawBoolean(raw.legacy_admission_enabled) ?? prior.legacyAdmissionEnabled,
    policyControlSupported: rawBoolean(raw.policy_control_supported) ?? prior.policyControlSupported,
    projected: prior.projected,
  }
}

export function applyPersistentMutationResult(state: RealtimeState, result: PersistentMutationResult): RealtimeState {
  if (!result.ok || !state.snapshot?.persistent) return state
  const persistent = state.snapshot.persistent
  const payload = result.payload
  let slots = persistent.slots
  const rawSlot = rawRecord(payload.slot)
  const logicalAgentId = rawString(rawSlot?.logical_agent_id)
  let touched = false
  if (logicalAgentId) {
    const prior = slots.find((item) => item.logicalAgentId === logicalAgentId)
    const next = slotPatch(prior, payload)
    if (next) {
      touched = true
      slots = next.state === 'deleted'
        ? slots.filter((item) => item.logicalAgentId !== logicalAgentId)
        : prior
          ? slots.map((item) => item.logicalAgentId === logicalAgentId ? next : item)
          : [...slots, next]
    }
  }
  const hasPolicy = Boolean(rawRecord(payload.policy))
  const policy = hasPolicy ? policyPatch(persistent.policy, payload.policy) : persistent.policy
  touched = touched || hasPolicy
  if (!touched) return state
  const scopes = [
    ...(logicalAgentId ? ['logical_agent'] : []),
    ...(hasPolicy ? ['persistent_policy'] : []),
  ]
  return {
    ...state,
    snapshot: {
      ...state.snapshot,
      persistent: {
        ...persistent,
        serverNow: rawString(payload.server_now) ?? persistent.serverNow,
        policy,
        slots,
      },
    },
    freshness: state.freshness === 'stale' ? 'stale' : 'catching_up',
    catchingUpScopes: state.freshness === 'stale'
      ? state.catchingUpScopes
      : scopes.reduce((values, scope) => addScope(values, scope), state.catchingUpScopes),
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
