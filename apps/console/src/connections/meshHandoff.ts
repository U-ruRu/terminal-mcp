/** Public intent only. Credentials are fetched after each fresh authenticated read. */
export type PendingMeshHandoff = {
  nodeId: string
  sourceControlId: string
  targetControlId: string
  targetFleetId: string
  targetMeshId: string
}

const PREFIX = 'terminal-mcp.console.pending-mesh-handoff.v1:'

export function readMeshHandoff(storage: Pick<Storage, 'getItem'>, id: string): PendingMeshHandoff | null {
  const value = storage.getItem(PREFIX + id)
  if (!value) return null
  const parsed = JSON.parse(value) as Partial<PendingMeshHandoff>
  if (['nodeId', 'sourceControlId', 'targetControlId', 'targetFleetId', 'targetMeshId']
    .some((field) => typeof parsed[field as keyof PendingMeshHandoff] !== 'string'
      || !parsed[field as keyof PendingMeshHandoff])) throw new Error('control_commit_uncertain')
  return parsed as PendingMeshHandoff
}

export function beginMeshHandoff(storage: Pick<Storage, 'getItem' | 'setItem'>, id: string, intent: PendingMeshHandoff): void {
  const previous = readMeshHandoff(storage, id)
  if (previous && Object.keys(intent).some((key) => previous[key as keyof PendingMeshHandoff] !== intent[key as keyof PendingMeshHandoff])) {
    throw new Error('control_commit_uncertain')
  }
  // Storage failure prevents Detach, so a crash cannot lose the requested target.
  storage.setItem(PREFIX + id, JSON.stringify(intent))
}

export function finishMeshHandoff(storage: Pick<Storage, 'getItem' | 'removeItem'>, id: string, meshId: string): void {
  if (readMeshHandoff(storage, id)?.targetMeshId === meshId) storage.removeItem(PREFIX + id)
}
