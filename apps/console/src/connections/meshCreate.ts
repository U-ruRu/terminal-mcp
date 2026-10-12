/** Durable, non-secret idempotency identity for a single unresolved Create Mesh. */
export type PendingMeshCreate = {
  meshId: string
  displayName: string
  controlNodeId: string
}

const PREFIX = 'terminal-mcp.console.pending-mesh-create.v1:'

function key(instanceId: string): string {
  return PREFIX + instanceId
}

export function beginMeshCreate(
  storage: Pick<Storage, 'getItem' | 'setItem'>,
  instanceId: string,
  controlNodeId: string,
  displayName: string,
): PendingMeshCreate {
  const previous = storage.getItem(key(instanceId))
  if (previous !== null) {
    try {
      const parsed = JSON.parse(previous) as Partial<PendingMeshCreate>
      if (parsed.controlNodeId === controlNodeId && parsed.displayName === displayName
        && typeof parsed.meshId === 'string' && /^mesh-[0-9a-f]{32}$/.test(parsed.meshId)) {
        return parsed as PendingMeshCreate
      }
    } catch { /* An unresolved unknown result is never silently replaced. */ }
    throw new Error('mesh_create_reconcile_required')
  }
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  const meshId = 'mesh-' + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  const pending = { meshId, controlNodeId, displayName }
  // Must persist before the mutation. Failure prevents the unsafe untracked POST.
  storage.setItem(key(instanceId), JSON.stringify(pending))
  return pending
}

export function readPendingMeshCreate(
  storage: Pick<Storage, 'getItem'>,
  instanceId: string,
): PendingMeshCreate | null {
  const value = storage.getItem(key(instanceId))
  if (!value) return null
  try {
    const parsed = JSON.parse(value) as Partial<PendingMeshCreate>
    if (typeof parsed.meshId === 'string' && /^mesh-[0-9a-f]{32}$/.test(parsed.meshId)
      && typeof parsed.controlNodeId === 'string' && typeof parsed.displayName === 'string') {
      return parsed as PendingMeshCreate
    }
  } catch { /* An invalid journal must remain unresolved. */ }
  return null
}

export function finishMeshCreate(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  instanceId: string,
  meshId: string,
): void {
  const previous = storage.getItem(key(instanceId))
  if (!previous) return
  try {
    const parsed = JSON.parse(previous) as Partial<PendingMeshCreate>
    if (parsed.meshId === meshId) storage.removeItem(key(instanceId))
  } catch { /* Preserve an ambiguous journal until explicitly reconciled. */ }
}
