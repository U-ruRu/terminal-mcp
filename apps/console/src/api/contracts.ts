import type {
  ActivityEventReadModel,
  ActivityFeedReadModel,
  ActivityMessageReadModel,
  AgentCollectionReadModel,
  AgentReadModel,
  CommunicationReadModel,
  ConsoleSnapshotReadModel,
  ContextCollectionReadModel,
  ContextReadModel,
  HostResourcesReadModel,
  IntentReadModel,
  JsonRecord,
  MessageReadModel,
  TaskCollectionReadModel,
  TaskOwnerReadModel,
  TaskReadModel,
  WebSocketTicketReadModel,
} from './models'

export class ConsoleContractError extends Error {
  constructor(readonly path: string, message: string) {
    super(`Invalid Terminal MCP Console response at ${path}: ${message}`)
    this.name = 'ConsoleContractError'
  }
}

function record(value: unknown, path: string): JsonRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ConsoleContractError(path, 'expected object')
  }
  return value as JsonRecord
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new ConsoleContractError(path, 'expected array')
  return value
}

function string(value: unknown, path: string): string {
  if (typeof value !== 'string') throw new ConsoleContractError(path, 'expected string')
  return value
}

function optionalString(value: unknown, path: string): string | undefined {
  if (value === undefined || value === null) return undefined
  return string(value, path)
}

function integer(value: unknown, path: string): number {
  if (!Number.isInteger(value) || (value as number) < 0) {
    throw new ConsoleContractError(path, 'expected non-negative integer')
  }
  return value as number
}

function optionalInteger(value: unknown, path: string): number | undefined {
  if (value === undefined || value === null) return undefined
  return integer(value, path)
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') throw new ConsoleContractError(path, 'expected boolean')
  return value
}

function number(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new ConsoleContractError(path, 'expected non-negative finite number')
  }
  return value
}

function optionalNumber(value: unknown, path: string): number | undefined {
  if (value === undefined || value === null) return undefined
  return number(value, path)
}

function resources(value: unknown, path: string): HostResourcesReadModel {
  const root = record(value, path)
  const cpu = record(root.cpu, path + '.cpu')
  const memory = record(root.memory, path + '.memory')
  const filesystem = record(root.filesystem, path + '.filesystem')
  const uptime = record(root.uptime, path + '.uptime')
  const resourceStatus = (raw: unknown, itemPath: string) =>
    enumValue(raw, ['available', 'unavailable'] as const, itemPath)
  return {
    status: enumValue(root.status, ['available', 'partial', 'unavailable'] as const, path + '.status'),
    cpu: {
      status: resourceStatus(cpu.status, path + '.cpu.status'),
      logicalCores: optionalInteger(cpu.logical_cores, path + '.cpu.logical_cores'),
      usagePercent: optionalNumber(cpu.usage_percent, path + '.cpu.usage_percent'),
      load1m: optionalNumber(cpu.load_1m, path + '.cpu.load_1m'),
      load5m: optionalNumber(cpu.load_5m, path + '.cpu.load_5m'),
      load15m: optionalNumber(cpu.load_15m, path + '.cpu.load_15m'),
    },
    memory: {
      status: resourceStatus(memory.status, path + '.memory.status'),
      totalBytes: optionalInteger(memory.total_bytes, path + '.memory.total_bytes'),
      usedBytes: optionalInteger(memory.used_bytes, path + '.memory.used_bytes'),
      availableBytes: optionalInteger(memory.available_bytes, path + '.memory.available_bytes'),
      usedPercent: optionalNumber(memory.used_percent, path + '.memory.used_percent'),
    },
    filesystem: {
      status: resourceStatus(filesystem.status, path + '.filesystem.status'),
      totalBytes: optionalInteger(filesystem.total_bytes, path + '.filesystem.total_bytes'),
      usedBytes: optionalInteger(filesystem.used_bytes, path + '.filesystem.used_bytes'),
      freeBytes: optionalInteger(filesystem.free_bytes, path + '.filesystem.free_bytes'),
      usedPercent: optionalNumber(filesystem.used_percent, path + '.filesystem.used_percent'),
    },
    uptime: {
      status: resourceStatus(uptime.status, path + '.uptime.status'),
      seconds: optionalInteger(uptime.seconds, path + '.uptime.seconds'),
    },
  }
}

function stringArray(value: unknown, path: string): string[] {
  return array(value, path).map((item, index) => string(item, `${path}[${index}]`))
}

function optionalStringArray(value: unknown, path: string): string[] {
  if (value === undefined || value === null) return []
  return stringArray(value, path)
}

function enumValue<T extends string>(
  value: unknown,
  allowed: readonly T[],
  path: string,
): T {
  const parsed = string(value, path)
  if (!allowed.includes(parsed as T)) {
    throw new ConsoleContractError(path, `expected one of ${allowed.join(', ')}`)
  }
  return parsed as T
}

function owner(value: unknown, path: string): TaskOwnerReadModel | undefined {
  if (value === undefined || value === null) return undefined
  const item = record(value, path)
  return {
    agentId: optionalString(item.agent_id, `${path}.agent_id`),
    agentName: string(item.agent_name, `${path}.agent_name`),
    claimedAt: string(item.claimed_at, `${path}.claimed_at`),
    claimAgeSeconds: integer(item.claim_age_seconds, `${path}.claim_age_seconds`),
    claimIntent: string(item.claim_intent, `${path}.claim_intent`),
    role: enumValue(item.role, ['owner', 'participant'] as const, `${path}.role`),
  }
}

function agentIntentScope(value: unknown, path: string) {
  const item = record(value, path)
  return {
    instanceId: string(item.instance_id, path + '.instance_id'),
    intent: string(item.intent, path + '.intent'),
    currentStep: integer(item.current_step, path + '.current_step'),
    updatedAt: string(item.updated_at, path + '.updated_at'),
    ageSeconds: integer(item.age_seconds, path + '.age_seconds'),
    status: enumValue(item.status, ['fresh', 'stale'] as const, path + '.status'),
    local: boolean(item.local, path + '.local'),
  }
}

function agent(value: unknown, path: string): AgentReadModel {
  const item = record(value, path)
  return {
    agentId: optionalString(item.agent_id, `${path}.agent_id`),
    name: string(item.name, `${path}.name`),
    status: string(item.status, `${path}.status`),
    intent: string(item.intent, `${path}.intent`),
    currentStep: integer(item.current_step, `${path}.current_step`),
    lastActivity: string(item.last_activity, path + '.last_activity'),
    lastActivityAt: string(item.last_activity_at, path + '.last_activity_at'),
    logicalLastActivityAt: optionalString(
      item.logical_last_activity_at,
      path + '.logical_last_activity_at',
    ),
    logicalIdleSeconds: optionalInteger(
      item.logical_idle_seconds,
      path + '.logical_idle_seconds',
    ),
    logicalSessionStatus:
      item.logical_session_status === undefined
        ? undefined
        : enumValue(
            item.logical_session_status,
            ['active', 'finished', 'forced'] as const,
            path + '.logical_session_status',
          ),
    localIntentStatus:
      item.local_intent_status === undefined || item.local_intent_status === null
        ? undefined
        : enumValue(
            item.local_intent_status,
            ['fresh', 'stale', 'missing'] as const,
            path + '.local_intent_status',
          ),
    intentScopes: array(item.intent_scopes ?? [], path + '.intent_scopes').map(
      (raw, index) => agentIntentScope(raw, path + '.intent_scopes[' + index + ']'),
    ),
    idleSeconds: optionalInteger(item.idle_seconds, path + '.idle_seconds'),
    sessionAgeSeconds: optionalInteger(item.session_age_seconds, `${path}.session_age_seconds`),
    sessionRemainingSeconds: optionalInteger(
      item.session_remaining_seconds,
      `${path}.session_remaining_seconds`,
    ),
    sessionRef: optionalString(item.session_ref, `${path}.session_ref`),
    originInstanceId: optionalString(item.origin_instance_id, `${path}.origin_instance_id`),
    sessionStartedAt: optionalString(item.session_started_at, `${path}.session_started_at`),
    attachment: typeof item.attachment === 'boolean' ? item.attachment : undefined,
    taskSummary: optionalString(item.task_summary, `${path}.task_summary`),
    workScope: optionalStringArray(item.work_scope, `${path}.work_scope`),
    messagesAwaitingRead: optionalInteger(
      item.messages_awaiting_read,
      `${path}.messages_awaiting_read`,
    ) ?? 0,
    messagesAwaitingReply: optionalInteger(
      item.messages_awaiting_reply,
      `${path}.messages_awaiting_reply`,
    ) ?? 0,
    alertsPending: optionalInteger(item.alerts_pending, `${path}.alerts_pending`) ?? 0,
  }
}

function task(value: unknown, path: string): TaskReadModel {
  const item = record(value, path)
  const namespace = string(item.namespace, `${path}.namespace`)
  const taskId = string(item.task_id, `${path}.task_id`)
  const known = new Set([
    'namespace', 'task_id', 'title', 'lane', 'priority', 'state', 'operational_status',
    'active', 'archived_at', 'tags', 'next_action', 'candidate_ref', 'owner', 'checkpoint',
    'result',
  ])
  const details: JsonRecord = {}
  for (const [key, raw] of Object.entries(item)) {
    if (!known.has(key)) details[key] = raw
  }

  return {
    key: `${namespace}/${taskId}`,
    namespace,
    taskId,
    title: string(item.title, `${path}.title`),
    lane: enumValue(
      item.lane,
      ['implementation', 'review', 'release', 'integration', 'general'] as const,
      `${path}.lane`,
    ),
    priority: enumValue(item.priority, ['P0', 'P1', 'P2', 'P3'] as const, `${path}.priority`),
    state: enumValue(item.state, ['ready', 'blocked', 'deferred', 'done'] as const, `${path}.state`),
    operationalStatus: enumValue(
      item.operational_status,
      ['ready', 'in_progress', 'blocked', 'deferred', 'done'] as const,
      `${path}.operational_status`,
    ),
    active: boolean(item.active, `${path}.active`),
    archived: item.archived_at !== undefined && item.archived_at !== null,
    tags: stringArray(item.tags ?? [], `${path}.tags`),
    nextAction: string(item.next_action ?? '', `${path}.next_action`),
    candidateRef: optionalString(item.candidate_ref, `${path}.candidate_ref`),
    owner: owner(item.owner, `${path}.owner`),
    participants: array(item.participants ?? [], `${path}.participants`).map((raw, index) =>
      owner(raw, `${path}.participants[${index}]`),
    ).filter((item): item is TaskOwnerReadModel => item !== undefined),
    checkpoint: item.checkpoint ?? {},
    result: item.result,
    details: Object.keys(details).length > 0 ? details : undefined,
  }
}

function contextEntries(value: unknown, primary: boolean, path: string): ContextReadModel[] {
  if (value === undefined || value === null) return []
  return array(value, path).map((raw, index) => {
    const item = record(raw, `${path}[${index}]`)
    return {
      id: integer(item.id, `${path}[${index}].id`),
      summary: string(item.summary, `${path}[${index}].summary`),
      content: optionalString(item.content, `${path}[${index}].content`),
      primary,
    }
  })
}

function message(value: unknown, path: string): MessageReadModel {
  const item = record(value, path)
  return {
    messageHash: string(item.message_hash, `${path}.message_hash`),
    state: string(item.state, `${path}.state`),
    senderName: string(item.sender_name, `${path}.sender_name`),
    text: string(item.text, `${path}.text`),
    requireReply: typeof item.require_reply === 'boolean' ? item.require_reply : false,
    alert: typeof item.alert === 'boolean' ? item.alert : false,
    createdAt: string(item.created_at, `${path}.created_at`),
  }
}

function intent(value: unknown, path: string): IntentReadModel {
  const item = record(value, path)
  return {
    timestamp: string(item.timestamp, `${path}.timestamp`),
    intent: string(item.intent, `${path}.intent`),
    step: integer(item.step, `${path}.step`),
    workScope: optionalStringArray(item.work_scope, `${path}.work_scope`),
  }
}

function communication(value: unknown, path: string): CommunicationReadModel {
  const item = record(value, path)
  return {
    agentId: optionalString(item.agent_id, `${path}.agent_id`),
    name: string(item.name, `${path}.name`),
    sessionRef: optionalString(item.session_ref, `${path}.session_ref`),
    messagesAwaitingRead: integer(item.messages_awaiting_read ?? 0, `${path}.messages_awaiting_read`),
    messagesAwaitingReply: integer(
      item.messages_awaiting_reply ?? 0,
      `${path}.messages_awaiting_reply`,
    ),
    alertsPending: integer(item.alerts_pending ?? 0, `${path}.alerts_pending`),
    messageJournal: array(item.message_journal ?? [], `${path}.message_journal`).map(
      (raw, index) => message(raw, `${path}.message_journal[${index}]`),
    ),
    intentJournal: array(item.intent_journal ?? [], `${path}.intent_journal`).map(
      (raw, index) => intent(raw, `${path}.intent_journal[${index}]`),
    ),
  }
}

export function decodeAgents(value: unknown, path = '$'): AgentCollectionReadModel {
  const root = record(value, path)
  if (root.ok !== true) throw new ConsoleContractError(`${path}.ok`, 'expected true')
  return {
    agents: array(root.sessions ?? [], `${path}.sessions`).map((raw, index) =>
      agent(raw, `${path}.sessions[${index}]`),
    ),
  }
}

export function decodeTasks(value: unknown, path = '$'): TaskCollectionReadModel {
  const root = record(value, path)
  if (root.ok !== true) throw new ConsoleContractError(`${path}.ok`, 'expected true')
  const tagCountsRaw = record(root.tag_counts ?? {}, `${path}.tag_counts`)
  const tagCounts: Record<string, number> = {}
  for (const [key, raw] of Object.entries(tagCountsRaw)) {
    tagCounts[key] = integer(raw, `${path}.tag_counts.${key}`)
  }
  return {
    tasks: array(root.tasks ?? [], `${path}.tasks`).map((raw, index) =>
      task(raw, `${path}.tasks[${index}]`),
    ),
    summary: record(root.summary ?? {}, `${path}.summary`),
    tagCounts,
  }
}

export function decodeTaskDetail(value: unknown, path = '$'): TaskReadModel {
  const root = record(value, path)
  if (root.ok !== true) throw new ConsoleContractError(`${path}.ok`, 'expected true')
  if (!root.task) throw new ConsoleContractError(`${path}.task`, 'expected task')
  return task(root.task, `${path}.task`)
}

export function decodeContexts(value: unknown, path = '$'): ContextCollectionReadModel {
  const root = record(value, path)
  if (root.ok !== true) throw new ConsoleContractError(`${path}.ok`, 'expected true')
  return {
    contexts: [
      ...contextEntries(root.primary, true, `${path}.primary`),
      ...contextEntries(root.additional, false, `${path}.additional`),
    ],
  }
}

export function decodeSnapshot(value: unknown, path = '$'): ConsoleSnapshotReadModel {
  const root = record(value, path)
  if (root.ok !== true) throw new ConsoleContractError(`${path}.ok`, 'expected true')

  const consistency = record(root.consistency, `${path}.consistency`)
  const mode = string(consistency.mode, `${path}.consistency.mode`)
  if (mode !== 'cursor_first_at_least_once') {
    throw new ConsoleContractError(`${path}.consistency.mode`, 'unsupported consistency mode')
  }
  const highWaterSeq = integer(root.high_water_seq, `${path}.high_water_seq`)
  const replayFromSeq = integer(consistency.replay_from_seq, `${path}.consistency.replay_from_seq`)
  if (integer(consistency.high_water_seq, `${path}.consistency.high_water_seq`) !== highWaterSeq) {
    throw new ConsoleContractError(`${path}.consistency.high_water_seq`, 'must match high_water_seq')
  }

  const instance = record(root.instance, `${path}.instance`)
  const health = record(instance.health, `${path}.instance.health`)
  const agents = decodeAgents({ ok: true, ...record(root.agents, `${path}.agents`) }, `${path}.agents`)
  const tasks = decodeTasks({ ok: true, ...record(root.tasks, `${path}.tasks`) }, `${path}.tasks`)
  const contexts = decodeContexts(
    { ok: true, ...record(root.contexts, `${path}.contexts`) },
    `${path}.contexts`,
  )

  return {
    highWaterSeq,
    replayFromSeq,
    duplicateEventsPossible: boolean(
      consistency.duplicate_events_possible,
      `${path}.consistency.duplicate_events_possible`,
    ),
    instance: {
      application: string(instance.application, `${path}.instance.application`),
      version: string(instance.version, `${path}.instance.version`),
      publicBaseUrl: string(instance.public_base_url, `${path}.instance.public_base_url`),
      healthy: health.ok === true,
      health,
      resources: resources(instance.resources, path + '.instance.resources'),
    },
    agents: agents.agents,
    tasks: tasks.tasks,
    contexts: contexts.contexts,
    communications: array(root.communications ?? [], `${path}.communications`).map(
      (raw, index) => communication(raw, `${path}.communications[${index}]`),
    ),
  }
}

export function decodeWebSocketTicket(
  value: unknown,
  path = '$',
): WebSocketTicketReadModel {
  const root = record(value, path)
  return {
    ticket: string(root.ticket, `${path}.ticket`),
    expiresIn: integer(root.expires_in, `${path}.expires_in`),
  }
}

function activityMessage(value: unknown, path: string): ActivityMessageReadModel {
  const item = record(value, path)
  return {
    messageHash: string(item.message_hash, `${path}.message_hash`),
    senderAgentId: optionalString(item.sender_agent_id, `${path}.sender_agent_id`),
    senderName: string(item.sender_name, `${path}.sender_name`),
    target: string(item.target, `${path}.target`),
    text: string(item.text, `${path}.text`),
    requireReply: boolean(item.require_reply, `${path}.require_reply`),
    alert: boolean(item.alert, `${path}.alert`),
    taskNamespace: optionalString(item.task_namespace, `${path}.task_namespace`),
    taskId: optionalString(item.task_id, `${path}.task_id`),
    recipients: array(item.recipients ?? [], `${path}.recipients`).map((raw, index) => {
      const recipient = record(raw, `${path}.recipients[${index}]`)
      return {
        agentId: optionalString(recipient.agent_id, `${path}.recipients[${index}].agent_id`),
        name: string(recipient.name, `${path}.recipients[${index}].name`),
        seen: boolean(recipient.seen, `${path}.recipients[${index}].seen`),
        read: boolean(recipient.read, `${path}.recipients[${index}].read`),
        replied: boolean(recipient.replied, `${path}.recipients[${index}].replied`),
      }
    }),
  }
}

function activityEvent(value: unknown, path: string): ActivityEventReadModel {
  const item = record(value, path)
  return {
    seq: integer(item.seq, `${path}.seq`),
    eventType: string(item.event_type, `${path}.event_type`),
    entityType: string(item.entity_type, `${path}.entity_type`),
    entityId: string(item.entity_id, `${path}.entity_id`),
    actorId: optionalString(item.actor_id, `${path}.actor_id`),
    actorName: optionalString(item.actor_name, `${path}.actor_name`),
    payload: record(item.payload ?? {}, `${path}.payload`),
    createdAt: string(item.created_at, `${path}.created_at`),
    message: item.message === undefined ? undefined : activityMessage(item.message, `${path}.message`),
  }
}

export function decodeActivityFeed(value: unknown, path = '$'): ActivityFeedReadModel {
  const root = record(value, path)
  if (root.ok !== true) throw new ConsoleContractError(`${path}.ok`, 'expected true')
  return {
    events: array(root.events ?? [], `${path}.events`).map((raw, index) =>
      activityEvent(raw, `${path}.events[${index}]`),
    ),
    since: integer(root.since, `${path}.since`),
    nextCursor: integer(root.next_cursor, `${path}.next_cursor`),
    oldestSeq: optionalInteger(root.oldest_seq, `${path}.oldest_seq`),
    highWaterSeq: integer(root.high_water_seq, `${path}.high_water_seq`),
    gap: boolean(root.gap, `${path}.gap`),
    gapFromSeq: optionalInteger(root.gap_from_seq, `${path}.gap_from_seq`),
    gapToSeq: optionalInteger(root.gap_to_seq, `${path}.gap_to_seq`),
  }
}
