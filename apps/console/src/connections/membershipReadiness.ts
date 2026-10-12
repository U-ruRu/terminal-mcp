import type { ManagedFleetControlReadModel, ManagedFleetNodeReadModel } from '../api/models'

export function isConverged(node: ManagedFleetNodeReadModel | undefined): boolean {
  return Boolean(node && !node.lastError
    && node.desiredTopologyRevision === node.appliedTopologyRevision
    && node.desiredTrustRevision === node.appliedTrustRevision
    && node.desiredPolicyRevision === node.appliedPolicyRevision)
}

export function nativeTrustReady(control: ManagedFleetControlReadModel | undefined, meshId: string): boolean {
  const trust = control?.nativeAccessTrust
  return Boolean(control?.managed && trust?.ready && trust.state === 'ready' && trust.meshId === meshId
    && trust.missingTransport.length === 0 && Object.values(trust.peers).every((state) => state === 'verified'))
}

/** Both inputs must be fresh direct reads, never a projected authority response. */
export function membershipReady(
  local: ManagedFleetControlReadModel | undefined,
  authority: ManagedFleetControlReadModel | undefined,
  nodeId: string,
  meshId: string,
): boolean {
  if (!local || !authority || local.nodeId !== nodeId || authority.nodeId !== authority.controlNodeId
    || local.fleetId !== authority.fleetId || local.controlNodeId !== authority.controlNodeId) return false
  const applied = local.nodes.find((node) => node.nodeId === nodeId)
  const desired = authority.nodes.find((node) => node.nodeId === nodeId)
  return Boolean(applied && desired && applied.state === 'active' && desired.state === 'active'
    && applied.meshId === meshId && desired.meshId === meshId && isConverged(applied) && isConverged(desired)
    && applied.appliedTopologyRevision >= desired.desiredTopologyRevision
    && applied.appliedTrustRevision >= desired.desiredTrustRevision
    && applied.appliedPolicyRevision >= desired.desiredPolicyRevision
    && nativeTrustReady(local, meshId)
    && nativeTrustReady(authority, authority.nodes.find((node) => node.nodeId === authority.nodeId)?.meshId ?? ''))
}

/** Synchronous click guard, independent of React render scheduling. */
export class MembershipMutationLifetime {
  private readonly pending = new Set<string>()
  has(id: string): boolean { return this.pending.has(id) }
  begin(id: string): void { this.pending.add(id) }
  finish(id: string): void { this.pending.delete(id) }
}
