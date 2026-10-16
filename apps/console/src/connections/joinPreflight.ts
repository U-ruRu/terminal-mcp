import type { ManagedFleetControlReadModel } from '../api/models'

/** A namespace is public metadata; enrollment authenticates the transition. */
export function isAutonomous(target: ManagedFleetControlReadModel): boolean {
  return target.controlNodeId === target.nodeId
    && target.nodes.every((node) => node.state === 'detached'
      || (node.nodeId === target.nodeId && !node.meshId))
    && !target.meshes.some((mesh) => mesh.adopted)
}

export function joinPreflightError(
  target: ManagedFleetControlReadModel,
  destination: ManagedFleetControlReadModel,
): 'namespace_transition_unsupported' | 'management_contract_mismatch' | 'already_managed_by_other_authority' | null {
  if (target.fleetId !== destination.fleetId && !target.namespaceTransitionSupported) return 'namespace_transition_unsupported'
  if (target.schemaVersion !== destination.schemaVersion) return 'management_contract_mismatch'
  if (target.managed && target.controlNodeId !== destination.controlNodeId && !isAutonomous(target)) {
    return 'already_managed_by_other_authority'
  }
  return null
}
