import type { ManagedFleetControlReadModel } from '../api/models'

export type FleetControlFreshness = 'fresh' | 'stale' | 'unknown'

export type CachedFleetControl = {
  control: ManagedFleetControlReadModel
  observedAt: number
}

type CacheDocument = {
  version: 1
  entries: Record<string, CachedFleetControl>
}

export const FLEET_CONTROL_CACHE_KEY = 'terminal-mcp.console.fleet-control.v1'

function browserStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function isControl(value: unknown): value is ManagedFleetControlReadModel {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item = value as Record<string, unknown>
  return (
    typeof item.schemaVersion === 'number'
    && typeof item.fleetId === 'string'
    && typeof item.nodeId === 'string'
    && typeof item.controlNodeId === 'string'
    && typeof item.managed === 'boolean'
    && Array.isArray(item.meshes)
    && Array.isArray(item.nodes)
    && Boolean(item.revisions && typeof item.revisions === 'object')
    && typeof item.updatedAt === 'string'
  )
}

function readDocument(storage = browserStorage()): CacheDocument {
  if (!storage) return { version: 1, entries: {} }
  try {
    const raw = storage.getItem(FLEET_CONTROL_CACHE_KEY)
    if (!raw) return { version: 1, entries: {} }
    const parsed = JSON.parse(raw) as Partial<CacheDocument>
    if (parsed.version !== 1 || !parsed.entries || typeof parsed.entries !== 'object') {
      return { version: 1, entries: {} }
    }
    const entries: Record<string, CachedFleetControl> = {}
    for (const [instanceId, entry] of Object.entries(parsed.entries)) {
      if (!entry || typeof entry !== 'object') continue
      const candidate = entry as Partial<CachedFleetControl>
      if (!isControl(candidate.control) || typeof candidate.observedAt !== 'number') continue
      entries[instanceId] = { control: candidate.control, observedAt: candidate.observedAt }
    }
    return { version: 1, entries }
  } catch {
    return { version: 1, entries: {} }
  }
}

function writeDocument(document: CacheDocument, storage = browserStorage()): void {
  if (!storage) return
  try {
    storage.setItem(FLEET_CONTROL_CACHE_KEY, JSON.stringify(document))
  } catch {
    // Control cache is only a last-known projection; storage failure must not break live control reads.
  }
}

export function loadCachedFleetControl(instanceId: string): CachedFleetControl | undefined {
  return readDocument().entries[instanceId]
}

export function saveCachedFleetControl(
  instanceId: string,
  control: ManagedFleetControlReadModel,
  observedAt = Date.now(),
): void {
  const document = readDocument()
  document.entries[instanceId] = { control, observedAt }
  writeDocument(document)
}

export function propagateCachedFleetControl(
  authoritative: ManagedFleetControlReadModel,
  observedAt = Date.now(),
): void {
  const document = readDocument()
  let changed = false
  for (const [instanceId, entry] of Object.entries(document.entries)) {
    if (entry.control.fleetId !== authoritative.fleetId) continue
    const nodeId = entry.control.nodeId
    const node = authoritative.nodes.find((candidate) => candidate.nodeId === nodeId)
    const mesh = node?.meshId
      ? authoritative.meshes.find((candidate) => candidate.meshId === node.meshId)
      : undefined
    document.entries[instanceId] = {
      observedAt,
      control: {
        ...authoritative,
        nodeId,
        mesh,
      },
    }
    changed = true
  }
  if (changed) writeDocument(document)
}
