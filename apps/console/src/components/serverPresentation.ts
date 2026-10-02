import type { FleetServerReadModel } from '../fleet/readModel'

export type ResourceKind = 'cpu' | 'memory' | 'filesystem'
export type ResourceVisualState = 'normal' | 'attention' | 'unavailable'
export type ServerVisualState = 'healthy' | 'attention' | 'critical' | 'stale' | 'loading' | 'offline'

export const RESOURCE_THRESHOLDS = {
  cpu: 90,
  memory: 80,
  filesystem: 80,
} as const

export function resourcePercent(server: FleetServerReadModel, kind: ResourceKind): number | undefined {
  if (server.connectivity === 'offline') return undefined
  const resources = server.resources
  if (!resources) return undefined
  if (kind === 'cpu') {
    if (resources.cpu.status !== 'available') return undefined
    if (resources.cpu.usagePercent !== undefined) return resources.cpu.usagePercent
    const cores = resources.cpu.logicalCores
    const load = resources.cpu.load1m
    return cores && load !== undefined ? (load / cores) * 100 : undefined
  }
  const resource = kind === 'memory' ? resources.memory : resources.filesystem
  return resource.status === 'available' ? resource.usedPercent : undefined
}

export function resourceVisualState(server: FleetServerReadModel, kind: ResourceKind): ResourceVisualState {
  if (server.connectivity === 'offline') return 'unavailable'
  const resources = server.resources
  if (!resources) return 'unavailable'

  const available =
    kind === 'cpu'
      ? resources.cpu.status === 'available'
      : kind === 'memory'
        ? resources.memory.status === 'available'
        : resources.filesystem.status === 'available'
  if (!available) return 'unavailable'

  const value = resourcePercent(server, kind)
  return value !== undefined && value >= RESOURCE_THRESHOLDS[kind] ? 'attention' : 'normal'
}

export function resourceDisplayValue(
  server: FleetServerReadModel,
  kind: ResourceKind,
  unavailable: string,
  loadLabel: string,
): string {
  if (server.connectivity === 'offline') return unavailable
  const resources = server.resources
  if (!resources) return unavailable

  void loadLabel
  const value = resourcePercent(server, kind)
  return value === undefined ? unavailable : Math.round(value) + '%'
}

export function hasResourceAttention(server: FleetServerReadModel): boolean {
  return (['cpu', 'memory', 'filesystem'] as const).some(
    (kind) => resourceVisualState(server, kind) === 'attention',
  )
}

export function serverVisualState(server: FleetServerReadModel): ServerVisualState {
  if (server.connectivity === 'offline') return 'offline'
  if (server.healthy === false) return 'critical'

  if (hasResourceAttention(server)) return 'attention'

  if (server.freshness === 'stale') return 'stale'
  if (server.freshness === 'catching_up' || server.connectivity === 'connecting' || server.connectivity === 'reconnecting') {
    return 'loading'
  }
  return 'healthy'
}

export function needsAttention(server: FleetServerReadModel): boolean {
  const state = serverVisualState(server)
  return state === 'offline' || state === 'critical' || state === 'attention' || state === 'stale'
}

export function serverAlphaSort(a: FleetServerReadModel, b: FleetServerReadModel): number {
  return a.displayName.localeCompare(b.displayName) || a.instanceId.localeCompare(b.instanceId)
}

const severity: Record<ServerVisualState, number> = {
  offline: 0,
  critical: 0,
  attention: 1,
  stale: 2,
  loading: 3,
  healthy: 4,
}

export function serverProblemSort(a: FleetServerReadModel, b: FleetServerReadModel): number {
  return severity[serverVisualState(a)] - severity[serverVisualState(b)] || serverAlphaSort(a, b)
}
