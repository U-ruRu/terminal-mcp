import type {
  AgentReadModel,
  CommunicationReadModel,
  ConsoleSnapshotReadModel,
  ContextReadModel,
  HostResourcesReadModel,
  TaskReadModel,
} from '../api/models'
import type { InstanceEvent, RealtimeStatus } from '../realtime/state'
import type { FleetInstanceView } from './types'

export type FleetFreshness = 'loading' | 'fresh' | 'catching_up' | 'stale' | 'offline'
export type FleetConnectionState = 'loading' | 'live' | 'offline'
export type FleetHealthState = 'unknown' | 'healthy' | 'attention'

export type FleetSource = {
  instanceId: string
  origin: string
  displayName: string
}

export type FleetAgentItem = FleetSource & {
  agent: AgentReadModel
}

export type FleetSessionAttachment = FleetSource & {
  status: string
  intent: string
  taskSummary?: string
  currentStep: number
  lastActivityAt: string
  attachment?: boolean
  localIntentStatus?: 'fresh' | 'stale' | 'missing'
}

export type FleetScopedIntent = {
  instanceId: string
  displayName: string
  intent: string
  currentStep: number
  updatedAt: string
  ageSeconds: number
  status: 'fresh' | 'stale'
}

export type FleetSessionReadModel = {
  sessionRef: string
  name: string
  originInstanceId?: string
  sessionAgeSeconds?: number
  sessionRemainingSeconds?: number
  attachments: FleetSessionAttachment[]
  scopedIntents: FleetScopedIntent[]
}

export type FleetTaskItem = FleetSource & {
  task: TaskReadModel
}

export type FleetActivityItem = FleetSource & {
  kind: 'event' | 'message'
  occurredAt: string
  label: string
  detail: string
  sequence?: number
}

export type FleetTaskCounts = {
  ready: number
  inProgress: number
  blocked: number
  deferred: number
  done: number
  highPriorityOpen: number
}

export type FleetCommunicationCounts = {
  unread: number
  replyRequired: number
  alerts: number
}

export type FleetServerReadModel = FleetSource & {
  connectivity: RealtimeStatus
  connectionState: FleetConnectionState
  freshness: FleetFreshness
  healthState: FleetHealthState
  version?: string
  healthy?: boolean
  resources?: HostResourcesReadModel
  contexts?: ContextReadModel[]
  lastSeenAt?: string
  reconnectAttempt: number
  lastError?: string
  staleReason?: string
  catchingUpScopes: string[]
  activeAgentCount: number
  activeIntents: string[]
  taskCounts: FleetTaskCounts
  communication: FleetCommunicationCounts
  blockerCount: number
  snapshotAvailable: boolean
}

export type FleetReadModel = {
  servers: FleetServerReadModel[]
  summary: {
    totalServers: number
    liveServers: number
    staleServers: number
    offlineServers: number
    activeAgents: number
    blockedTasks: number
    replyRequired: number
    alerts: number
  }
  activeAgents: FleetAgentItem[]
  sessions: FleetSessionReadModel[]
  blockers: FleetTaskItem[]
  recentActivity: FleetActivityItem[]
}

function sourceOf(instance: FleetInstanceView): FleetSource {
  return {
    instanceId: instance.profile.instanceId,
    origin: instance.profile.origin,
    displayName: instance.profile.displayName,
  }
}

function connectionState(instance: FleetInstanceView): FleetConnectionState {
  const realtime = instance.runtime.realtime
  if (realtime?.socketConnected) return 'live'
  if (instance.runtime.status === 'offline') return 'offline'
  return 'loading'
}

function freshness(instance: FleetInstanceView): FleetFreshness {
  const realtime = instance.runtime.realtime
  if (!realtime?.snapshot) return 'loading'
  if (realtime.freshness === 'catching_up') return 'catching_up'
  if (realtime.freshness === 'stale' || instance.runtime.status !== 'live') return 'stale'
  return 'fresh'
}

function healthState(snapshot: ConsoleSnapshotReadModel | null, counts: FleetTaskCounts, communication: FleetCommunicationCounts): FleetHealthState {
  if (snapshot?.instance.healthy === false || counts.blocked > 0 || communication.alerts > 0 || communication.replyRequired > 0) return 'attention'
  if (snapshot?.instance.healthy === true) return 'healthy'
  return 'unknown'
}

function taskCounts(tasks: TaskReadModel[]): FleetTaskCounts {
  const counts: FleetTaskCounts = {
    ready: 0,
    inProgress: 0,
    blocked: 0,
    deferred: 0,
    done: 0,
    highPriorityOpen: 0,
  }

  for (const task of tasks) {
    if (task.archived) continue
    if (task.operationalStatus === 'ready') counts.ready += 1
    else if (task.operationalStatus === 'in_progress') counts.inProgress += 1
    else if (task.operationalStatus === 'blocked') counts.blocked += 1
    else if (task.operationalStatus === 'deferred') counts.deferred += 1
    else if (task.operationalStatus === 'done') counts.done += 1

    if (
      task.operationalStatus !== 'done' &&
      (task.priority === 'P0' || task.priority === 'P1')
    ) {
      counts.highPriorityOpen += 1
    }
  }

  return counts
}

function communicationCounts(
  communications: CommunicationReadModel[],
): FleetCommunicationCounts {
  return communications.reduce<FleetCommunicationCounts>(
    (counts, item) => ({
      unread: counts.unread + item.messagesAwaitingRead,
      replyRequired: counts.replyRequired + item.messagesAwaitingReply,
      alerts: counts.alerts + item.alertsPending,
    }),
    { unread: 0, replyRequired: 0, alerts: 0 },
  )
}

function maxIso(values: Array<string | undefined>): string | undefined {
  let latest: string | undefined
  for (const value of values) {
    if (!value) continue
    if (!latest || value > latest) latest = value
  }
  return latest
}

function snapshotLastSeen(
  snapshot: ConsoleSnapshotReadModel,
  lastEvent?: InstanceEvent,
): string | undefined {
  const agentTimes = snapshot.agents.map((agent) => agent.lastActivityAt)
  const messageTimes = snapshot.communications.flatMap((communication) =>
    communication.messageJournal.map((message) => message.createdAt),
  )
  return maxIso([lastEvent?.createdAt, ...agentTimes, ...messageTimes])
}

function sourceSort(a: FleetSource, b: FleetSource): number {
  return (
    a.displayName.localeCompare(b.displayName) ||
    a.origin.localeCompare(b.origin) ||
    a.instanceId.localeCompare(b.instanceId)
  )
}

function activitySort(a: FleetActivityItem, b: FleetActivityItem): number {
  return (
    b.occurredAt.localeCompare(a.occurredAt) ||
    sourceSort(a, b) ||
    (b.sequence ?? -1) - (a.sequence ?? -1) ||
    a.label.localeCompare(b.label)
  )
}

function statusRank(status: RealtimeStatus): number {
  if (status === 'offline') return 0
  if (status === 'stale') return 1
  if (status === 'reconnecting') return 2
  if (status === 'connecting') return 3
  return 4
}

function serverSort(a: FleetServerReadModel, b: FleetServerReadModel): number {
  return (
    statusRank(a.connectivity) - statusRank(b.connectivity) ||
    b.blockerCount - a.blockerCount ||
    b.communication.alerts - a.communication.alerts ||
    b.communication.replyRequired - a.communication.replyRequired ||
    sourceSort(a, b)
  )
}

function eventActivity(
  source: FleetSource,
  event: InstanceEvent | undefined,
): FleetActivityItem[] {
  if (!event) return []
  return [
    {
      ...source,
      kind: 'event',
      occurredAt: event.createdAt,
      label: event.eventType,
      detail: event.entityType + ':' + event.entityId,
      sequence: event.seq,
    },
  ]
}

function messageActivity(
  source: FleetSource,
  snapshot: ConsoleSnapshotReadModel | null,
): FleetActivityItem[] {
  if (!snapshot) return []
  return snapshot.communications.flatMap((communication) =>
    communication.messageJournal.map((message) => ({
      ...source,
      kind: 'message' as const,
      occurredAt: message.createdAt,
      label: message.alert
        ? 'alert'
        : message.requireReply
          ? 'reply_required'
          : 'message',
      detail: message.senderName + ': ' + message.text,
    })),
  )
}

export function buildFleetReadModel(
  instances: FleetInstanceView[],
  activityLimit = 20,
): FleetReadModel {
  const servers: FleetServerReadModel[] = []
  const activeAgents: FleetAgentItem[] = []
  const sessionsByRef = new Map<string, FleetSessionReadModel>()
  const blockers: FleetTaskItem[] = []
  const recentActivity: FleetActivityItem[] = []

  for (const instance of instances) {
    const source = sourceOf(instance)
    const snapshot = instance.runtime.realtime?.snapshot ?? null
    const agents = snapshot?.agents ?? []
    const tasks = snapshot?.tasks ?? []
    const communications = snapshot?.communications ?? []
    const counts = taskCounts(tasks)
    const communication = communicationCounts(communications)

    for (const agent of agents) {
      if (agent.status !== 'active') continue
      activeAgents.push({ ...source, agent })

      const sessionRef =
        agent.sessionRef ??
        source.instanceId + ':' + agent.name + ':' + (agent.sessionStartedAt ?? agent.lastActivityAt)
      const attachment: FleetSessionAttachment = {
        ...source,
        status: agent.status,
        intent: agent.intent,
        taskSummary: agent.taskSummary,
        currentStep: agent.currentStep,
        lastActivityAt: agent.logicalLastActivityAt ?? agent.lastActivityAt,
        attachment: agent.attachment,
        localIntentStatus: agent.localIntentStatus,
      }
      const scopedIntents = (agent.intentScopes ?? []).map((scope) => ({
        instanceId: scope.instanceId,
        displayName:
          instances.find((candidate) => candidate.profile.instanceId === scope.instanceId)
            ?.profile.displayName ?? scope.instanceId,
        intent: scope.intent,
        currentStep: scope.currentStep,
        updatedAt: scope.updatedAt,
        ageSeconds: scope.ageSeconds,
        status: scope.status,
      }))
      const current = sessionsByRef.get(sessionRef)
      if (current) {
        current.attachments.push(attachment)
        for (const scope of scopedIntents) {
          const existing = current.scopedIntents.find((item) => item.instanceId === scope.instanceId)
          if (!existing) {
            current.scopedIntents.push(scope)
          } else if (
            scope.updatedAt > existing.updatedAt ||
            (scope.updatedAt === existing.updatedAt && scope.intent > existing.intent)
          ) {
            Object.assign(existing, scope)
          }
        }
        if (agent.originInstanceId && !current.originInstanceId) {
          current.originInstanceId = agent.originInstanceId
        }
        if (agent.sessionAgeSeconds !== undefined) {
          current.sessionAgeSeconds = Math.max(
            current.sessionAgeSeconds ?? agent.sessionAgeSeconds,
            agent.sessionAgeSeconds,
          )
        }
        if (agent.sessionRemainingSeconds !== undefined) {
          current.sessionRemainingSeconds = Math.min(
            current.sessionRemainingSeconds ?? agent.sessionRemainingSeconds,
            agent.sessionRemainingSeconds,
          )
        }
      } else {
        sessionsByRef.set(sessionRef, {
          sessionRef,
          name: agent.name,
          originInstanceId: agent.originInstanceId,
          sessionAgeSeconds: agent.sessionAgeSeconds,
          sessionRemainingSeconds: agent.sessionRemainingSeconds,
          attachments: [attachment],
          scopedIntents,
        })
      }
    }

    for (const task of tasks) {
      if (task.archived || task.operationalStatus !== 'blocked') continue
      blockers.push({ ...source, task })
    }

    recentActivity.push(
      ...eventActivity(source, instance.runtime.realtime?.lastEvent),
      ...messageActivity(source, snapshot),
    )

    servers.push({
      ...source,
      connectivity: instance.runtime.status,
      connectionState: connectionState(instance),
      freshness: freshness(instance),
      healthState: healthState(snapshot, counts, communication),
      version: snapshot?.instance.version,
      healthy: snapshot?.instance.healthy,
      resources: snapshot?.instance.resources,
      contexts: snapshot?.contexts ?? [],
      lastSeenAt: snapshot
        ? snapshotLastSeen(snapshot, instance.runtime.realtime?.lastEvent)
        : undefined,
      reconnectAttempt: instance.runtime.reconnectAttempt,
      lastError: instance.runtime.lastError,
      staleReason: instance.runtime.realtime?.staleReason,
      catchingUpScopes: instance.runtime.realtime?.catchingUpScopes ?? [],
      activeAgentCount: agents.filter((agent) => agent.status === 'active').length,
      activeIntents: agents
        .filter((agent) => agent.status === 'active')
        .flatMap((agent) => {
          const scopes = agent.intentScopes ?? []
          if (scopes.length === 0) return agent.intent ? [agent.intent] : []
          const local = scopes.find(
            (scope) => scope.instanceId === source.instanceId && scope.status === 'fresh',
          )
          return local?.intent ? [local.intent] : []
        })
        .sort((a, b) => a.localeCompare(b)),
      taskCounts: counts,
      communication,
      blockerCount: counts.blocked,
      snapshotAvailable: snapshot !== null,
    })
  }

  servers.sort(serverSort)
  const sessions = [...sessionsByRef.values()]
  for (const session of sessions) {
    session.attachments.sort(sourceSort)
    session.scopedIntents.sort(
      (a, b) => a.displayName.localeCompare(b.displayName) || a.instanceId.localeCompare(b.instanceId),
    )
  }
  sessions.sort((a, b) => a.name.localeCompare(b.name) || a.sessionRef.localeCompare(b.sessionRef))
  activeAgents.sort(
    (a, b) =>
      sourceSort(a, b) ||
      a.agent.name.localeCompare(b.agent.name) ||
      a.agent.intent.localeCompare(b.agent.intent),
  )
  blockers.sort(
    (a, b) =>
      sourceSort(a, b) ||
      a.task.priority.localeCompare(b.task.priority) ||
      a.task.taskId.localeCompare(b.task.taskId),
  )
  recentActivity.sort(activitySort)

  return {
    servers,
    summary: {
      totalServers: servers.length,
      liveServers: servers.filter((server) => server.connectivity === 'live').length,
      staleServers: servers.filter(
        (server) =>
          server.connectivity !== 'live' && server.connectivity !== 'offline',
      ).length,
      offlineServers: servers.filter((server) => server.connectivity === 'offline')
        .length,
      activeAgents: sessions.length,
      blockedTasks: blockers.length,
      replyRequired: servers.reduce(
        (total, server) => total + server.communication.replyRequired,
        0,
      ),
      alerts: servers.reduce(
        (total, server) => total + server.communication.alerts,
        0,
      ),
    },
    activeAgents,
    sessions,
    blockers,
    recentActivity: recentActivity.slice(0, Math.max(0, Math.floor(activityLimit))),
  }
}
