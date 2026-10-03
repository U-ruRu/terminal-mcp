import type {
  AgentReadModel,
  ConsoleSnapshotReadModel,
  ContextReadModel,
  HostResourcesReadModel,
  PersistentClaimReadModel,
  PersistentConsoleReadModel,
  PersistentSlotReadModel,
  PersistentWorkSessionReadModel,
  TaskOwnerReadModel,
  TaskReadModel,
} from '../api/models'
import type { ConnectionProfile } from '../connections/types'
import type { RealtimeStatus } from '../realtime/state'
import type { FleetInstanceView } from './types'
import type { FleetCacheView, FleetProjectionEntity } from './v1Types'

export type FleetSourceBinding = { sourceNodeId: string; profile: ConnectionProfile }

const priorityMap: Record<number, TaskReadModel['priority']> = {
  3: 'P0', 2: 'P1', 1: 'P2', 0: 'P3',
}
const text = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback
const integer = (value: unknown, fallback = 0) => Number.isInteger(value) ? Number(value) : fallback
const strings = (value: unknown) => Array.isArray(value)
  ? value.filter((item): item is string => typeof item === 'string')
  : []
const priority = (value: unknown) => priorityMap[integer(value)] ?? 'P3'
function lane(value: unknown): TaskReadModel['lane'] {
  const item = String(value)
  return ['implementation', 'review', 'release', 'integration', 'general'].includes(item)
    ? item as TaskReadModel['lane'] : 'general'
}
function state(value: unknown): TaskReadModel['state'] {
  const item = String(value)
  return ['ready', 'blocked', 'deferred', 'done'].includes(item)
    ? item as TaskReadModel['state'] : 'ready'
}
const rows = (cache: FleetCacheView, source: string, type: string) => {
  const values = new Map(
    cache.entities
      .filter((item) => item.sourceNodeId === source && item.entityType === type)
      .map((item) => [item.entityId, { ...item, payload: { ...item.payload } }]),
  )
  const writes = (cache.confirmedWrites ?? [])
    .filter((item) => item.sourceNodeId === source && item.entityType === type)
    .sort((a, b) => a.createdAt - b.createdAt || a.requestId.localeCompare(b.requestId))
  for (const write of writes) {
    if (write.remove) {
      values.delete(write.entityId)
      continue
    }
    const current = values.get(write.entityId)
    values.set(write.entityId, {
      sourceNodeId: source,
      entityType: type,
      entityId: write.entityId,
      entityRevision: Math.max(current?.entityRevision ?? 0, write.entityRevision),
      payloadVersion: current?.payloadVersion ?? 2,
      payload: { ...(current?.payload ?? {}), ...write.payloadPatch },
      authorityNodeId: current?.authorityNodeId,
      authorityEpoch: write.authorityEpoch ?? current?.authorityEpoch,
      sourceStreamGeneration: current?.sourceStreamGeneration ?? '',
      sourceSeq: current?.sourceSeq ?? 0,
      projectionSeq: current?.projectionSeq ?? cache.appliedProjectionSeq,
      updatedAt: current?.updatedAt ?? new Date(write.createdAt).toISOString(),
    })
  }
  return [...values.values()]
}

function latestSession(
  sessions: FleetProjectionEntity[],
  logicalAgentId: string,
): FleetProjectionEntity | undefined {
  return sessions
    .filter((item) => text(item.payload.logical_agent_id) === logicalAgentId)
    .sort((a, b) => integer(b.payload.session_epoch) - integer(a.payload.session_epoch))
    .find((item) => !item.payload.ended_at && ['active', 'stopping'].includes(text(item.payload.state)))
}

function workSession(item?: FleetProjectionEntity): PersistentWorkSessionReadModel | undefined {
  if (!item) return undefined
  const p = item.payload
  return {
    workSessionId: text(p.work_session_id, item.entityId),
    sessionEpoch: integer(p.session_epoch, 1),
    authorityNodeId: text(p.authority_node_id, item.authorityNodeId ?? ''),
    authorityEpoch: integer(p.authority_epoch, item.authorityEpoch ?? 1),
    startedAt: text(p.started_at),
    hardExpiresAt: text(p.hard_expires_at),
    state: text(p.state),
    originInstanceId: text(p.origin_instance_id) || undefined,
  }
}

function claimsFor(
  claims: FleetProjectionEntity[],
  logicalAgentId: string,
  tasks: Map<string, FleetProjectionEntity>,
): PersistentClaimReadModel[] {
  return claims
    .filter((item) =>
      !item.payload.released_at &&
      text(item.payload.owner_kind) === 'logical_agent' &&
      text(item.payload.owner_id) === logicalAgentId
    )
    .map((item) => {
      const namespace = text(item.payload.namespace)
      const taskId = text(item.payload.task_id)
      const task = tasks.get(namespace + '/' + taskId)
      return {
        namespace,
        taskId,
        lane: text(task?.payload.lane, 'general'),
        priority: priority(task?.payload.priority),
        state: text(task?.payload.state, 'ready'),
        claimedAt: text(item.payload.claimed_at),
        claimIntent: text(item.payload.claim_intent),
      }
    })
}

function persistent(
  cache: FleetCacheView,
  source: string,
  nowIso: string,
): PersistentConsoleReadModel {
  const sessions = rows(cache, source, 'work_session')
  const attachments = rows(cache, source, 'node_attachment')
  const claims = rows(cache, source, 'work_claim')
  const tasks = new Map(rows(cache, source, 'task').map((item) => [item.entityId, item]))
  const slots: PersistentSlotReadModel[] = rows(cache, source, 'logical_agent').map((item) => {
    const p = item.payload
    const id = text(p.logical_agent_id, item.entityId)
    const session = workSession(latestSession(sessions, id))
    return {
      logicalAgentId: id,
      displayName: typeof p.display_name === 'string' ? p.display_name : '',
      state: text(p.state, 'suspended'),
      authorityNodeId: text(p.authority_node_id, item.authorityNodeId ?? source),
      authorityEpoch: integer(p.authority_epoch, item.authorityEpoch ?? 1),
      slotRevision: integer(p.slot_revision, item.entityRevision),
      selector: '—',
      selectorGeneration: integer(p.selector_generation),
      authGeneration: integer(p.auth_generation),
      access: text(p.public_name) ? {
        publicName: text(p.public_name),
        accessGeneration: integer(p.access_generation),
        status: text(p.access_status, 'active'),
      } : undefined,
      createdAt: text(p.created_at, item.updatedAt),
      updatedAt: text(p.updated_at, item.updatedAt),
      serverNow: nowIso,
      workSession: session,
      claims: claimsFor(claims, id, tasks),
      audit: [],
      attachments: attachments
        .filter((entry) =>
          !entry.payload.revoked_at &&
          text(entry.payload.logical_agent_id) === id &&
          (!session || text(entry.payload.work_session_id) === session.workSessionId)
        )
        .map((entry) => ({
          nodeAttachmentId: text(entry.payload.node_attachment_id, entry.entityId),
          nodeInstanceId: text(entry.payload.node_instance_id),
          attachedAt: text(entry.payload.attached_at, entry.updatedAt),
          hardExpiresAt: text(entry.payload.hard_expires_at, session?.hardExpiresAt ?? ''),
        })),
    }
  })
  return {
    enabled: true,
    available: true,
    serverNow: nowIso,
    policy: {
      durationSeconds: 0,
      warningAfterSeconds: 0,
      alertAfterSeconds: 0,
      rearmAfterSeconds: 180,
      manualRearm: true,
      admissionMode: 'fleet-projected',
      legacyAdmissionEnabled: false,
      policyControlSupported: false,
      projected: true,
    },
    slots,
  }
}

function tasks(cache: FleetCacheView, source: string, nowMs: number): TaskReadModel[] {
  const names = new Map(rows(cache, source, 'logical_agent').map((item) => [
    text(item.payload.logical_agent_id, item.entityId),
    text(item.payload.display_name, item.entityId),
  ]))
  const claims = rows(cache, source, 'work_claim').filter((item) => !item.payload.released_at)
  return rows(cache, source, 'task').map((item) => {
    const p = item.payload
    const namespace = text(p.namespace)
    const taskId = text(p.task_id)
    const taskState = state(p.state)
    const claim = claims.find((entry) =>
      text(entry.payload.namespace) === namespace &&
      text(entry.payload.task_id) === taskId
    )
    let owner: TaskOwnerReadModel | undefined
    if (claim) {
      const ownerId = text(claim.payload.owner_id || claim.payload.agent_id)
      const claimedAt = text(claim.payload.claimed_at)
      const stamp = Date.parse(claimedAt)
      owner = {
        agentId: ownerId || undefined,
        agentName: names.get(ownerId) ?? ownerId,
        claimedAt,
        claimAgeSeconds: Number.isFinite(stamp)
          ? Math.max(0, Math.floor((nowMs - stamp) / 1000)) : 0,
        claimIntent: text(claim.payload.claim_intent),
        role: 'owner',
      }
    }
    return {
      key: namespace + '/' + taskId,
      namespace,
      taskId,
      title: text(p.title, taskId),
      lane: lane(p.lane),
      priority: priority(p.priority),
      state: taskState,
      operationalStatus: taskState === 'done' ? 'done' : claim ? 'in_progress' : taskState,
      active: Boolean(claim),
      archived: Boolean(p.archived_at),
      tags: strings(p.tags),
      nextAction: text(p.next_action),
      candidateRef: text(p.candidate_ref) || undefined,
      owner,
      participants: [],
      checkpoint: p.checkpoint ?? {},
      result: p.result,
      details: { revision: integer(p.revision, item.entityRevision) },
    }
  })
}

function agents(cache: FleetCacheView, source: string, nowMs: number): AgentReadModel[] {
  const sessions = rows(cache, source, 'work_session')
  const presence = rows(cache, source, 'attachment_presence')
  return rows(cache, source, 'logical_agent').map((item) => {
    const p = item.payload
    const id = text(p.logical_agent_id, item.entityId)
    const session = workSession(latestSession(sessions, id))
    const scoped = presence.filter((entry) =>
      text(entry.payload.logical_agent_id) === id &&
      (!session || text(entry.payload.work_session_id) === session.workSessionId)
    )
    const latest = [...scoped].sort(
      (a, b) => text(b.payload.intent_updated_at).localeCompare(text(a.payload.intent_updated_at)),
    )[0]
    const last = text(latest?.payload.last_activity_at, item.updatedAt)
    const started = session ? Date.parse(session.startedAt) : NaN
    const hard = session ? Date.parse(session.hardExpiresAt) : NaN
    return {
      agentId: id,
      name: text(p.display_name, id),
      status: text(p.state, 'suspended'),
      intent: text(latest?.payload.intent),
      currentStep: integer(latest?.payload.current_step),
      lastActivity: last,
      lastActivityAt: last,
      logicalLastActivityAt: last || undefined,
      logicalIdleSeconds: last ? Math.max(0, Math.floor((nowMs - Date.parse(last)) / 1000)) : undefined,
      logicalSessionStatus: session ? 'active' : undefined,
      localIntentStatus: latest ? 'fresh' : 'missing',
      intentScopes: scoped.map((entry) => ({
        instanceId: text(entry.payload.node_instance_id, entry.sourceNodeId),
        intent: text(entry.payload.intent),
        currentStep: integer(entry.payload.current_step),
        updatedAt: text(entry.payload.intent_updated_at, entry.updatedAt),
        ageSeconds: Math.max(0, Math.floor(
          (nowMs - Date.parse(text(entry.payload.intent_updated_at, entry.updatedAt))) / 1000,
        )),
        status: 'fresh' as const,
        local: text(entry.payload.node_instance_id) === source,
      })),
      sessionAgeSeconds: Number.isFinite(started)
        ? Math.max(0, Math.floor((nowMs - started) / 1000)) : undefined,
      sessionRemainingSeconds: Number.isFinite(hard)
        ? Math.max(0, Math.floor((hard - nowMs) / 1000)) : undefined,
      sessionRef: session?.workSessionId,
      originInstanceId: session?.originInstanceId,
      sessionStartedAt: session?.startedAt,
      attachment: scoped.length > 0,
      taskSummary: text(latest?.payload.task_summary) || undefined,
      workScope: strings(latest?.payload.work_scope),
      messagesAwaitingRead: 0,
      messagesAwaitingReply: 0,
      alertsPending: 0,
    }
  })
}

function contexts(cache: FleetCacheView, source: string): ContextReadModel[] {
  return rows(cache, source, 'context').map((item) => ({
    id: integer(item.payload.context_id, Number(item.entityId) || 0),
    summary: text(item.payload.summary),
    content: text(item.payload.content) || undefined,
    primary: item.payload.primary === true || item.payload.primary === 1,
  }))
}

function overlayDegraded(cache: FleetCacheView, source: string): boolean {
  const overlay = cache.runtimeOverlays.find((item) => item.sourceNodeId === source)
  if (!overlay || overlay.freshness !== 'fresh') return false
  return ['finalization_pending_commands', 'stale_running_commands', 'unowned_running_commands']
    .some((key) => Array.isArray(overlay.payload[key]) && (overlay.payload[key] as unknown[]).length > 0)
}

function unavailableResources(): HostResourcesReadModel {
  return {
    status: 'unavailable',
    cpu: { status: 'unavailable' },
    memory: { status: 'unavailable' },
    filesystem: { status: 'unavailable' },
    uptime: { status: 'unavailable' },
  }
}

function projectedResources(cache: FleetCacheView, source: string): HostResourcesReadModel {
  const overlay = cache.runtimeOverlays.find((item) => item.sourceNodeId === source)
  if (!overlay) return unavailableResources()
  const raw = overlay.payload.resources
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return unavailableResources()
  const data = raw as Record<string, unknown>
  const object = (value: unknown): Record<string, unknown> =>
    value && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {}
  const number = (value: unknown): number | undefined =>
    typeof value === 'number' && Number.isFinite(value) ? value : undefined
  const resourceStatus = (value: unknown): 'available' | 'unavailable' =>
    object(value).status === 'available' ? 'available' : 'unavailable'
  const cpu = object(data.cpu)
  const memory = object(data.memory)
  const filesystem = object(data.filesystem)
  const uptime = object(data.uptime)
  const overall = data.status === 'available' || data.status === 'partial'
    ? data.status
    : 'unavailable'
  return {
    status: overall,
    cpu: {
      status: resourceStatus(cpu),
      logicalCores: number(cpu.logical_cores),
      usagePercent: number(cpu.usage_percent),
      load1m: number(cpu.load_1m),
      load5m: number(cpu.load_5m),
      load15m: number(cpu.load_15m),
    },
    memory: {
      status: resourceStatus(memory),
      totalBytes: number(memory.total_bytes),
      usedBytes: number(memory.used_bytes),
      availableBytes: number(memory.available_bytes),
      usedPercent: number(memory.used_percent),
    },
    filesystem: {
      status: resourceStatus(filesystem),
      totalBytes: number(filesystem.total_bytes),
      usedBytes: number(filesystem.used_bytes),
      freeBytes: number(filesystem.free_bytes),
      usedPercent: number(filesystem.used_percent),
    },
    uptime: {
      status: resourceStatus(uptime),
      seconds: number(uptime.seconds),
    },
  }
}

function snapshot(
  cache: FleetCacheView,
  source: string,
  profile: ConnectionProfile,
  nowMs: number,
  sourceDegraded = overlayDegraded(cache, source),
): ConsoleSnapshotReadModel {
  const projectedAgents = agents(cache, source, nowMs)
  const runtimeOverlay = cache.runtimeOverlays.find((item) => item.sourceNodeId === source)
  const runtimePayload = runtimeOverlay?.payload ?? {}
  return {
    highWaterSeq: cache.appliedProjectionSeq,
    replayFromSeq: cache.appliedProjectionSeq,
    duplicateEventsPossible: false,
    instance: {
      application: text(runtimePayload.application, 'terminal-mcp'),
      version: text(runtimePayload.version),
      publicBaseUrl: profile.origin,
      healthy: !sourceDegraded,
      health: {
        source: 'fleet-v1',
        projection_epoch: cache.projectionEpoch,
        projection_seq: cache.appliedProjectionSeq,
      },
      resources: projectedResources(cache, source),
    },
    agents: projectedAgents,
    tasks: tasks(cache, source, nowMs),
    contexts: contexts(cache, source),
    communications: [],
    persistent: persistent(cache, source, new Date(nowMs).toISOString()),
  }
}

function status(
  freshness: string,
  fleetStatus: 'dormant' | 'restoring' | 'live' | 'degraded' | 'fallback',
  degraded: boolean,
): RealtimeStatus {
  if (freshness === 'unavailable') return 'offline'
  if (freshness !== 'fresh' || fleetStatus !== 'live' || degraded) return 'stale'
  return 'live'
}

const SYNTHETIC_FLEET_INSTANCE_PREFIX = 'fleet-source-'

export function projectedSourceNodeId(instanceId: string): string | undefined {
  if (!instanceId.startsWith(SYNTHETIC_FLEET_INSTANCE_PREFIX)) return undefined
  const sourceNodeId = instanceId.slice(SYNTHETIC_FLEET_INSTANCE_PREFIX.length)
  return sourceNodeId || undefined
}

function syntheticProfile(source: string): ConnectionProfile {
  return {
    instanceId: SYNTHETIC_FLEET_INSTANCE_PREFIX + source,
    origin: 'https://fleet.invalid',
    displayName: source,
    credentialRef: 'fleet-unbound-' + source,
    metadata: { deviceId: '', clientId: '', deviceLabel: 'Fleet projection', scope: '', pairedAt: 0 },
    createdAt: 0,
    updatedAt: 0,
  }
}

export function buildProjectedFleetInstances(
  cache: FleetCacheView,
  bindings: FleetSourceBinding[],
  fleetStatus: 'dormant' | 'restoring' | 'live' | 'degraded' | 'fallback',
  nowMs = Date.now(),
): FleetInstanceView[] {
  const bound = new Map(bindings.map((item) => [item.sourceNodeId, item.profile]))
  const sourceIds = new Set([
    ...cache.sources.map((item) => item.sourceNodeId),
    ...cache.entities.map((item) => item.sourceNodeId),
    ...(cache.scopeStatuses ?? []).map((item) => item.sourceNodeId),
    ...(cache.confirmedWrites ?? []).map((item) => item.sourceNodeId),
  ])
  return [...sourceIds].sort().map((source) => {
    const profile = bound.get(source) ?? syntheticProfile(source)
    const sourceFreshness = cache.sources.find((item) => item.sourceNodeId === source)?.freshness ?? 'stale'
    const scopeStatuses = (cache.scopeStatuses ?? []).filter((item) => item.sourceNodeId === source)
    const scopeOfflineAuth = scopeStatuses.some((item) => item.status === 'OFFLINE_AUTH')
    const catchingUpScopes = scopeStatuses.filter((item) => item.status === 'CATCHING_UP').map((item) => item.scope)
    const scopeDegraded = scopeStatuses.some((item) => item.status === 'DEGRADED')
    const effectiveFreshness = scopeOfflineAuth ? 'unavailable' : sourceFreshness
    const degraded = overlayDegraded(cache, source) || scopeDegraded
    const realtimeStatus = status(effectiveFreshness, fleetStatus, degraded)
    const realtimeFreshness = realtimeStatus !== 'live'
      ? 'stale' as const
      : catchingUpScopes.length > 0 ? 'catching_up' as const : 'fresh' as const
    return {
      profile,
      runtime: {
        instanceId: profile.instanceId,
        status: realtimeStatus,
        authStatus: bound.has(source) ? 'connected' : 'unpaired',
        realtime: {
          status: realtimeStatus,
          snapshot: snapshot(cache, source, profile, nowMs, degraded),
          cursor: cache.appliedProjectionSeq,
          highWaterSeq: cache.appliedProjectionSeq,
          socketConnected: fleetStatus === 'live',
          reconnectAttempt: 0,
          freshness: realtimeFreshness,
          catchingUpScopes,
          staleReason: realtimeStatus === 'stale'
            ? scopeDegraded ? 'fleet_scope_not_live'
              : degraded ? 'runtime_ownership_degraded' : 'fleet_projection_stale'
            : realtimeStatus === 'offline' && scopeOfflineAuth
              ? 'fleet_scope_offline_auth'
              : undefined,
        },
        reconnectAttempt: 0,
      },
    }
  })
}


export function projectedTask(
  cache: FleetCacheView,
  sourceNodeId: string,
  namespace: string,
  taskId: string,
  nowMs = Date.now(),
): TaskReadModel | undefined {
  return tasks(cache, sourceNodeId, nowMs).find(
    (item) => item.namespace === namespace && item.taskId === taskId,
  )
}

export function taskFromQueryItem(
  value: Record<string, unknown>,
): TaskReadModel {
  const namespace = text(value.namespace)
  const taskId = text(value.task_id)
  const taskState = state(value.state)
  return {
    key: namespace + '/' + taskId,
    namespace,
    taskId,
    title: text(value.title, taskId),
    lane: lane(value.lane),
    priority: priority(value.priority),
    state: taskState,
    operationalStatus: taskState,
    active: false,
    archived: Boolean(value.archived_at),
    tags: strings(value.tags),
    nextAction: text(value.next_action),
    candidateRef: text(value.candidate_ref) || undefined,
    participants: [],
    checkpoint: value.checkpoint ?? {},
    result: value.result,
    details: { revision: integer(value.revision) },
  }
}

export function projectedActivity(
  cache: FleetCacheView,
  sourceNodeId: string,
  options: { since?: number; before?: number; limit?: number } = {},
) {
  const limit = Math.max(1, options.limit ?? 100)
  const all = cache.activity
    .filter((item) => item.sourceNodeId === sourceNodeId)
    .sort((a, b) => a.projectionSeq - b.projectionSeq)
  let selected = all
  if (options.since !== undefined) {
    selected = selected.filter((item) => item.projectionSeq > options.since!)
  }
  if (options.before !== undefined) {
    selected = selected.filter((item) => item.projectionSeq < options.before!)
  }
  selected = options.before !== undefined ? selected.slice(-limit) : selected.slice(0, limit)
  const events = selected.map((item) => ({
    seq: item.projectionSeq,
    eventType: item.eventType,
    entityType: item.entityType,
    entityId: item.entityId,
    payload: {},
    createdAt: item.createdAt,
  }))
  const nextCursor = events.at(-1)?.seq ?? options.since ?? 0
  return {
    events,
    since: options.since ?? 0,
    nextCursor,
    oldestSeq: all[0]?.projectionSeq,
    highWaterSeq: cache.appliedProjectionSeq,
    gap: false,
  }
}
