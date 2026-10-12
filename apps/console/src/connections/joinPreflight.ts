import type { ManagedFleetControlReadModel } from '../api/models'

/** Safe, read-only gate. A foreign active authority must never be silently rehomed. */
export function joinPreflightError(
  target: ManagedFleetControlReadModel,
  destination: ManagedFleetControlReadModel,
): 'fleet_id_mismatch' | 'management_contract_mismatch' | 'already_managed_by_other_authority' | null {
  if (target.fleetId !== destination.fleetId) return 'fleet_id_mismatch'
  if (target.schemaVersion !== destination.schemaVersion) return 'management_contract_mismatch'
  if (target.managed && target.controlNodeId !== destination.controlNodeId) {
    return 'already_managed_by_other_authority'
  }
  return null
}
