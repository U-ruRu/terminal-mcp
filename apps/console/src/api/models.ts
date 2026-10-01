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

export type AgentIntentScopeReadModel = {
  instanceId: string
  intent: string
  currentStep: number
  updatedAt: string
  ageSeconds: number
  status: 'fresh' | 'stale'
  local: boolean
}

export type AgentReadModel = {
  agentId?: string
  name: string
  status: string
  intent: string
  currentStep: number
  lastActivity: string
  lastActivityAt: string
  logicalLastActivityAt?: string
  logicalIdleSeconds?: number
  logicalSessionStatus?: 'active' | 'finished' | 'forced'
  localIntentStatus?: 'fresh' | 'stale' | 'missing'
  intentScopes?: AgentIntentScopeReadModel[]
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
  agentId?: string
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
  state: 'ready' | 'in_progress' | 'blocked' | 'deferred' | 'done'
  operationalStatus: 'ready' | 'in_progress' | 'blocked' | 'deferred' | 'done'
  active: boolean
  archived: boolean
  tags: string[]
  nextAction: string
  candidateRef?: string
  owner?: TaskOwnerReadModel
  participants?: TaskOwnerReadModel[]
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
  agentId?: string
  name: string
  sessionRef?: string
  messagesAwaitingRead: number
  messagesAwaitingReply: number
  alertsPending: number
  messageJournal: MessageReadModel[]
  intentJournal: IntentReadModel[]
}

export type PersistentPolicyReadModel = {
  durationSeconds: number
  warningAfterSeconds: number
  alertAfterSeconds: number
  rearmAfterSeconds: number
  manualRearm: boolean
  admissionMode: string
  legacyAdmissionEnabled: boolean
  policyControlSupported: boolean
  projected?: boolean
}

export type PersistentClaimReadModel = {
  namespace: string
  taskId: string
  lane: string
  priority: string
  state: string
  claimedAt: string
  claimIntent: string
}

export type PersistentAuditReadModel = {
  id: number
  eventType: string
  principalId: string
  workSessionId?: string
  sessionEpoch?: number
  payload: JsonRecord
  createdAt: string
}

export type PersistentAttachmentReadModel = {
  nodeAttachmentId: string
  nodeInstanceId: string
  attachedAt: string
  hardExpiresAt: string
}

export type PersistentWorkSessionReadModel = {
  workSessionId: string
  sessionEpoch: number
  authorityNodeId: string
  authorityEpoch: number
  startedAt: string
  hardExpiresAt: string
  state: string
  originInstanceId?: string
}

export type PersistentSlotReadModel = {
  logicalAgentId: string
  displayName: string
  state: string
  authorityNodeId: string
  authorityEpoch: number
  slotRevision: number
  selector: string
  selectorGeneration: number
  authGeneration: number
  createdAt: string
  updatedAt: string
  serverNow: string
  workSession?: PersistentWorkSessionReadModel
  claims: PersistentClaimReadModel[]
  audit: PersistentAuditReadModel[]
  attachments: PersistentAttachmentReadModel[]
}

export type PersistentConsoleReadModel = {
  enabled: boolean
  available: boolean
  error?: string
  serverNow?: string
  policy: PersistentPolicyReadModel
  slots: PersistentSlotReadModel[]
}

export type PersistentMutationResult = {
  ok: boolean
  code?: string
  error?: string
  blockers?: JsonRecord[]
  payload: JsonRecord
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
  persistent?: PersistentConsoleReadModel
}

export type WebSocketTicketReadModel = {
  ticket: string
  expiresIn: number
}

export type ActivityRecipientReadModel = {
  agentId?: string
  name: string
  seen: boolean
  read: boolean
  replied: boolean
}

export type ActivityMessageReadModel = {
  messageHash: string
  senderAgentId?: string
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
  actorId?: string
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
