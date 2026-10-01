export type FleetProjectionSource = {
  sourceNodeId: string
  sourceStreamGeneration: string
  sourceSeq: number
  freshness: 'fresh' | 'stale' | 'reset_required' | 'unavailable'
  updatedAt: string
}

export type FleetProjectionEntity = {
  sourceNodeId: string
  entityType: string
  entityId: string
  entityRevision: number
  payloadVersion: number
  payload: Record<string, unknown>
  authorityNodeId?: string
  authorityEpoch?: number
  sourceStreamGeneration: string
  sourceSeq: number
  projectionSeq: number
  updatedAt: string
}

export type FleetRuntimeOverlay = {
  sourceNodeId: string
  payload: Record<string, unknown>
  observedAt: string
  freshness: 'fresh' | 'stale' | 'unavailable'
}

export type FleetScopeStatus = {
  sourceNodeId: string
  scope: string
  status: 'LIVE' | 'CATCHING_UP' | 'DEGRADED' | 'OFFLINE_AUTH'
  reason?: string
  updatedAt: string
}

export type FleetConfirmedWrite = {
  requestId: string
  sourceNodeId: string
  entityType: string
  entityId: string
  authorityEpoch?: number
  entityRevision: number
  payloadPatch: Record<string, unknown>
  remove?: boolean
  createdAt: number
  state: 'confirmed_pending_projection'
}

export type FleetProjectionSnapshot = {
  fleetId: string
  nodeId: string
  ownerNodeId: string
  role: 'owner' | 'follower'
  projectionEpoch: number
  projectionSeq: number
  updatedAt: string
  sources: FleetProjectionSource[]
  scopeStatuses?: FleetScopeStatus[]
  entities: FleetProjectionEntity[]
  runtimeOverlays: FleetRuntimeOverlay[]
}

export type FleetProjectionEvent = {
  projectionEpoch: number
  projectionSeq: number
  eventId: string
  sourceNodeId: string
  sourceStreamGeneration: string
  sourceSeq: number
  eventType: string
  entityType: string
  entityId: string
  entityRevision: number
  payloadVersion: number
  payload: Record<string, unknown>
  authorityNodeId?: string
  authorityEpoch?: number
  createdAt: string
}

export type FleetProjectionEventPage = {
  projectionEpoch: number
  projectionSeq: number
  oldestProjectionSeq?: number
  newestProjectionSeq?: number
  resetRequired: boolean
  events: FleetProjectionEvent[]
}

export type FleetCachedActivity = Pick<
  FleetProjectionEvent,
  'projectionSeq' | 'eventId' | 'sourceNodeId' | 'eventType' | 'entityType' | 'entityId' | 'createdAt'
>

export type FleetGatewayQuality = {
  candidateId: string
  ewmaRttMs?: number
  successRate: number
  reconnectRate: number
  completeness: number
  freshness: number
  softLoad?: number
  lastSuccessAt?: number
  lastFailureAt?: number
  probeCount: number
}

export type FleetDurableCacheState = {
  version: 1
  fleetId?: string
  projectionEpoch: number
  appliedProjectionSeq: number
  preferredIngressId?: string
  entities: FleetProjectionEntity[]
  sources: FleetProjectionSource[]
  activity: FleetCachedActivity[]
  gatewayQuality: FleetGatewayQuality[]
  scopeStatuses?: FleetScopeStatus[]
  confirmedWrites?: FleetConfirmedWrite[]
  updatedAt: number
}

export type FleetCacheView = FleetDurableCacheState & { runtimeOverlays: FleetRuntimeOverlay[] }

export type FleetQueryRequest = {
  sourceNodeIds?: string[]
  cursor?: string
  limit?: number
  q?: string
  filters?: Record<string, string | number | boolean>
  asOf?: string
  throughSeq?: number
  includeCount?: boolean
  includeFacets?: boolean
}

export type FleetRelaySource<T = unknown> = {
  sourceNodeId: string
  ok: boolean
  status: 'LIVE' | 'CATCHING_UP' | 'DEGRADED' | 'OFFLINE_AUTH'
  data?: T
  error?: string
}

export type FleetRelayResult<T = unknown> = {
  operation: string
  resource?: string
  sources: FleetRelaySource<T>[]
  partial: boolean
  complete: boolean
}

export function entityCacheKey(
  item: Pick<FleetProjectionEntity, 'sourceNodeId' | 'entityType' | 'entityId'>,
): string {
  return [item.sourceNodeId, item.entityType, item.entityId].join('\u0000')
}
