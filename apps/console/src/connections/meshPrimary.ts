import type { ManagedFleetControlReadModel, ManagedFleetMutationResult } from '../api/models'

type PrimaryIntent = { meshId: string; sourceNodeId: string; targetNodeId: string; operationId: string; topologyRevision: number }
type Journal = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
type ControlPort = {
  fleetControl(): Promise<ManagedFleetControlReadModel>
  fleetControlMutation(path: string, body: Record<string, unknown>): Promise<ManagedFleetMutationResult>
}
const PREFIX = 'terminal-mcp.console.mesh-primary.v1:'
const pendingFailure = (): ManagedFleetMutationResult => ({ ok: false, code: 'mesh_primary_handoff_pending', error: 'mesh_primary_handoff_pending' })

function readIntent(storage: Journal, meshId: string): PrimaryIntent | null {
  const raw = storage.getItem(PREFIX + meshId)
  if (raw === null) return null
  try {
    const value = JSON.parse(raw) as PrimaryIntent
    if (value.meshId === meshId && ['sourceNodeId', 'targetNodeId', 'operationId'].every((key) => typeof value[key as keyof PrimaryIntent] === 'string' && Boolean(value[key as keyof PrimaryIntent]))
      && Number.isSafeInteger(value.topologyRevision) && value.topologyRevision >= 0) return value
  } catch { /* An uncertain handoff must not be replaced with a new operation. */ }
  throw new Error('mesh_primary_handoff_pending')
}

function confirms(control: ManagedFleetControlReadModel, meshId: string, nodeId: string): boolean {
  return control.managed && control.controlNodeId === nodeId
    && control.nodes.some((node) => node.nodeId === nodeId && node.meshId === meshId && node.state === 'active')
}

/** Persist the exact request before transfer; fresh control confirms a lost receipt. */
export async function changeMeshPrimary(port: ControlPort, storage: Journal, meshId: string, nodeId: string): Promise<ManagedFleetMutationResult> {
  const current = await port.fleetControl()
  const previous = readIntent(storage, meshId)
  if (previous && previous.targetNodeId !== nodeId) return pendingFailure()
  if (confirms(current, meshId, nodeId)) {
    storage.removeItem(PREFIX + meshId)
    return { ok: true, control: current }
  }
  if (!current.managed || !current.nodes.some((node) => node.nodeId === nodeId && node.meshId === meshId && node.state === 'active')) {
    return { ok: false, code: 'membership_unknown', error: 'membership_unknown' }
  }
  if (previous && previous.sourceNodeId !== current.controlNodeId) return pendingFailure()
  const intent = previous ?? {
    meshId, sourceNodeId: current.controlNodeId, targetNodeId: nodeId,
    operationId: crypto.randomUUID(), topologyRevision: current.revisions.topology,
  }
  storage.setItem(PREFIX + meshId, JSON.stringify(intent))
  let result: ManagedFleetMutationResult
  try {
    result = await port.fleetControlMutation('/actions/fleet/control/primary', {
      node_id: intent.targetNodeId, operation_id: intent.operationId,
      expected_topology_revision: intent.topologyRevision,
    })
  } catch { result = pendingFailure() }
  try {
    const confirmed = await port.fleetControl()
    if (confirms(confirmed, meshId, nodeId)) {
      storage.removeItem(PREFIX + meshId)
      return { ok: true, control: confirmed }
    }
  } catch { /* Keep the durable intent until an authenticated read can confirm it. */ }
  // CAS rejection and a durable abort prove that this operation cannot commit.
  // The next attempt may use a fresh identity; uncertain transfers keep theirs.
  const code = result.code ?? result.error
  if (!result.ok && (code === 'topology_revision_conflict' || code === 'mesh_operation_aborted')) storage.removeItem(PREFIX + meshId)
  if (!result.ok && code === 'control_authority_required') return pendingFailure()
  return result.ok ? pendingFailure() : result
}
