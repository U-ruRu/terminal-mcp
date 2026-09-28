export type JsonRecord = Record<string, unknown>

export type ResourceStatus = 'available' | 'unavailable'

export type CpuResourceReadModel = {
  status: ResourceStatus
  logicalCores?: number
  usagePercent?: number
  load1m?: number
  load5m?: number
  load15m?: number
}

export type MemoryResourceReadModel = {
  status: ResourceStatus
  totalBytes?: number
  usedBytes?: number
  availableBytes?: number
  usedPercent?: number
}

export type FilesystemResourceReadModel = {
  status: ResourceStatus
  totalBytes?: number
  usedBytes?: number
  freeBytes?: number
  usedPercent?: number
}

export type UptimeResourceReadModel = {
  status: ResourceStatus
  seconds?: number
}

export type HostResourcesReadModel = {
  status: 'available' | 'partial' | 'unavailable'
  cpu: CpuResourceReadModel
  memory: MemoryResourceReadModel
  filesystem: FilesystemResourceReadModel
  uptime: UptimeResourceReadModel
}

export type InstanceReadModel = {
  application: string
  version: string
  publicBaseUrl: string
  healthy: boolean
  health: JsonRecord
  resources: HostResourcesReadModel
}

export type AgentReadModel = {
  name: string
  status: string
  intent: string
  currentStep: number
  lastActivity: string
  lastActivityAt: string
  idleSeconds?: number
  sessionAgeSeconds?: number
  sessionRemainingSeconds?: number
  sessionRef?: string
  originInstanceId?: string
  sessionStartedAt?: string
  attachment?: boolean
  taskSummary?: string
  workScope: string[]
  messagesAwaitingRead: number
  messagesAwaitingReply: number
  alertsPending: number
}

export type TaskOwnerReadModel = {
  agentName: string
  claimedAt: string
  claimAgeSeconds: number
  claimIntent: string
  role: 'owner' | 'participant'
}

export type TaskReadModel = {
  key: string
  namespace: string
  taskId: string
  title: string
  lane: 'implementation' | 'review' | 'release' | 'integration' | 'general'
  priority: 'P0' | 'P1' | 'P2' | 'P3'
  state: 'ready' | 'blocked' | 'deferred' | 'done'
  operationalStatus: 'ready' | 'in_progress' | 'blocked' | 'deferred' | 'done'
  active: boolean
  archived: boolean
  tags: string[]
  nextAction: string
  candidateRef?: string
  owner?: TaskOwnerReadModel
  checkpoint: unknown
  result?: unknown
  details?: JsonRecord
}

export type ContextReadModel = {
  id: number
  summary: string
  content?: string
  primary: boolean
}

export type MessageReadModel = {
  messageHash: string
  state: string
  senderName: string
  text: string
  requireReply: boolean
  alert: boolean
  createdAt: string
}

export type IntentReadModel = {
  timestamp: string
  intent: string
  step: number
  workScope: string[]
}

export type CommunicationReadModel = {
  name: string
  sessionRef?: string
  messagesAwaitingRead: number
  messagesAwaitingReply: number
  alertsPending: number
  messageJournal: MessageReadModel[]
  intentJournal: IntentReadModel[]
}

export type ConsoleSnapshotReadModel = {
  highWaterSeq: number
  replayFromSeq: number
  duplicateEventsPossible: boolean
  instance: InstanceReadModel
  agents: AgentReadModel[]
  tasks: TaskReadModel[]
  contexts: ContextReadModel[]
  communications: CommunicationReadModel[]
}

export type WebSocketTicketReadModel = {
  ticket: string
  expiresIn: number
}

export type ActivityRecipientReadModel = {
  name: string
  seen: boolean
  read: boolean
  replied: boolean
}

export type ActivityMessageReadModel = {
  messageHash: string
  senderName: string
  target: string
  text: string
  requireReply: boolean
  alert: boolean
  taskNamespace?: string
  taskId?: string
  recipients: ActivityRecipientReadModel[]
}

export type ActivityEventReadModel = {
  seq: number
  eventType: string
  entityType: string
  entityId: string
  actorName?: string
  payload: JsonRecord
  createdAt: string
  message?: ActivityMessageReadModel
}

export type ActivityFeedReadModel = {
  events: ActivityEventReadModel[]
  since: number
  nextCursor: number
  oldestSeq?: number
  highWaterSeq: number
  gap: boolean
  gapFromSeq?: number
  gapToSeq?: number
}

export type AgentCollectionReadModel = {
  agents: AgentReadModel[]
}

export type TaskCollectionReadModel = {
  tasks: TaskReadModel[]
  summary: JsonRecord
  tagCounts: Record<string, number>
}

export type ContextCollectionReadModel = {
  contexts: ContextReadModel[]
}
