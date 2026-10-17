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
): 'management_contract_mismatch' | null {
  return target.schemaVersion === destination.schemaVersion ? null : 'management_contract_mismatch'
}
