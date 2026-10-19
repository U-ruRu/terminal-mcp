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

export type WorkSessionReadModel = {
  workSessionId: string
  sessionEpoch: number
  authorityNodeId: string
  authorityEpoch: number
  startedAt: string
  hardExpiresAt: string
  state: string
  originInstanceId?: string
}

export type AccessMutationResult = {
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

export type ManagedFleetNodeReadModel = {
  nodeId: string
  origin?: string
  publicKey?: string
  meshId?: string
  state: 'active' | 'draining' | 'offline' | 'detached'
  desiredTopologyRevision: number
  appliedTopologyRevision: number
  desiredTrustRevision: number
  appliedTrustRevision: number
  desiredPolicyRevision: number
  appliedPolicyRevision: number
  lastError?: string
  updatedAt: string
}

export type ManagedAccessPolicyReadModel = {
  durationSeconds: number
  warningAfterSeconds: number
  alertAfterSeconds: number
  rearmAfterSeconds: number
  rearmEnabled: boolean
  legacyAdmissionEnabled: boolean
  revision: number
  updatedAt: string
}

export type ManagedFleetMeshReadModel = {
  meshId: string
  displayName: string
  adopted: boolean
  adoptedAt?: string
  updatedAt: string
}

export type NativeAccessTrustReadModel = {
  state: string
  ready: boolean
  meshId?: string
  peers: Record<string, string>
  missingTransport: string[]
}

export type ManagedFleetControlReadModel = {
  namespaceTransitionSupported?: boolean
  nativeAccessTrust?: NativeAccessTrustReadModel
  schemaVersion: number
  fleetId: string
  nodeId: string
  controlNodeId: string
  managed: boolean
  mesh?: ManagedFleetMeshReadModel
  meshes: ManagedFleetMeshReadModel[]
  nodes: ManagedFleetNodeReadModel[]
  policy?: ManagedAccessPolicyReadModel
  revisions: {
    routing: number
    topology: number
    trust: number
    accessPolicy: number
  }
  updatedAt: string
}

export type MeshVpnOffer = {
  payload: Record<string, unknown>
  signature: string
}

export type NodeRuntimeTarget = 'terminal' | 'tunnel'
export type NodeRuntimeOperation = {
  request_id: string
  target: NodeRuntimeTarget
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'unknown'
  created_at: number
  expires_at: number
  error: string | null
}
export type NodeRuntimeStatus = {
  supported: boolean
  platform: string
  restart_targets: NodeRuntimeTarget[]
  services: Partial<Record<NodeRuntimeTarget, { state: 'running' | 'stopped' | 'transitioning' | 'missing'; pid: number | null }>>
  operation: NodeRuntimeOperation | null
}

export type MeshVpnStatus = {
  ok: boolean
  node_id: string
  prepared: boolean
  backend: 'auto' | 'kernel' | 'userspace' | null
  overlay_ip: string | null
  endpoint: string | null
  tunnel: {
    interface: string
    running: boolean
    configured_backend: string
    peer_count: number
    handshakes: Record<string, number>
  } | null
  peers: Array<{ node_id: string; enrolled: boolean; mode: 'https' | 'wireguard' }>
  offer: MeshVpnOffer | null
}

export type MeshVpnResult = {
  ok: boolean
  code?: string
  error?: string
  restart_required?: boolean
  peer_id?: string
  transport?: 'https' | 'wireguard'
}

export type ManagedFleetEnrollment = {
  nodeId: string
  origin: string
  publicKey: string
  authToken: string
}

export type ManagedFleetMutationCompletion = {
  status: 'committed'
  convergence: 'pending' | 'converged'
  topologyRevision: number
}

export type ManagedFleetMutationResult = {
  ok: boolean
  code?: string
  error?: string
  control?: ManagedFleetControlReadModel
  mutation?: ManagedFleetMutationCompletion
}


/** Access slots share the current Mesh registry, or the local standalone registry. */
export type AccessMeshPolicyReadModel = {
  durationSeconds: number
  cooldownSeconds: number
  rearmEnabled: boolean
  warningSeconds: number
  drainingSeconds: number
  releaseOnEnd: boolean | null
}

export type AccessMeshSlotReadModel = {
  managementContractVersion?: number
  inheritanceSupported?: boolean
  inheritedPolicy?: AccessMeshPolicyReadModel
  inheritance?: SlotPolicyInheritanceReadModel
  codeGeneration?: number
  codeExpiresAt?: string | null
  createdAt?: string
  updatedAt?: string
  windowPolicy?: AccessMeshPolicyReadModel
  policyEffectiveAt?: string | null
  issuerNodeId: string
  slotId: string
  publicName: string
  mode: 'temporary' | 'persistent'
  state: string
  revision: number
  policy: AccessMeshPolicyReadModel
  sessionLifecycle: {
    state: string
    startedAt: string | null
    hardExpiresAt: string | null
    rearmAt: string | null
    remainingSeconds?: number
  }
}

export type AccessMeshDefaultsReadModel = {
  managementContractVersion?: number
  inheritanceSupported?: boolean
  meshPolicySupported?: boolean
  inheritedPolicy?: AccessMeshPolicyReadModel
  inheritance?: SlotPolicyInheritanceReadModel
  writeTarget?: 'fleet_control' | 'access_defaults'
  revision: number
  policy: AccessMeshPolicyReadModel
  legacyAdmissionEnabled: boolean
}

export type AccessMeshPolicyPatch = {
  duration_seconds?: number
  cooldown_seconds?: number
  rearm_enabled?: boolean
  warning_seconds?: number
  draining_seconds?: number
  release_on_end?: boolean | null
}

export type AccessMeshMutationRequest = { idempotency_key: string } & (
  | { action: 'create'; mode: 'temporary' | 'persistent'; policy?: AccessMeshPolicyPatch; inherit_policy?: boolean }
  | { action: 'defaults'; expected_revision: number; policy?: AccessMeshPolicyPatch; legacy_admission_enabled?: boolean; inherit_policy?: boolean }
  | { action: 'policy'; slot_id: string; expected_revision: number; policy?: AccessMeshPolicyPatch; inherit_policy?: boolean; expected_parent_revision?: number }
  | { action: 'deadline'; slot_id: string; expected_revision: number; deadline_at: string }
  | { action: 'suspend' | 'resume' | 'delete' | 'rotate' | 'end'; slot_id: string; expected_revision: number }
)


/** Authority identity and local convergence are separate from global freshness. */
export type SlotPolicyParentReadModel = {
  source: 'mesh' | 'standalone' | 'server'
  id: string
  revision: number | null
  version: string
  freshness: 'authoritative' | 'last_known'
  convergence: 'converged' | 'pending' | 'unknown'
}

export type SlotPolicyInheritanceReadModel = {
  mode: 'stored' | 'override' | 'inherit'
  source: 'slot' | 'mesh' | 'standalone'
  parent: SlotPolicyParentReadModel | null
  resetSupported: boolean
  appliesTo: 'next_window' | 'current_schedule'
  pending: boolean
}
