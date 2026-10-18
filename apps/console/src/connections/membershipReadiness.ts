import type { ManagedFleetControlReadModel, ManagedFleetNodeReadModel } from '../api/models'
import type { FleetControlFreshness } from './controlState'
import type { ControlObservation } from './controlObservation'
import { isAutonomous } from './joinPreflight'

export type MutationPhase = 'pending' | 'confirmed' | 'failed'
export type MembershipMutation = { targetMeshId: string; phase: MutationPhase; error?: string }
export type MembershipProjection = { ready?: boolean; kind: 'mesh' | 'standalone' | 'unknown'; meshId?: string; node?: ManagedFleetNodeReadModel; freshness: FleetControlFreshness }
export type OperationalState = 'current' | 'applying' | 'unavailable' | 'attention'

export function membershipTransitionPending(error: string | undefined): boolean {
  return error === 'mesh_transition_pending'
}

/** A transition receipt records intent. Fresh applied state confirms its result. */
export function resolvedMembershipMutation(
  membership: MembershipProjection,
  mutation: MembershipMutation | undefined,
): MembershipMutation | undefined {
  if (!mutation) return mutation
  const pending: MembershipMutation = mutation.phase === 'failed' && membershipTransitionPending(mutation.error)
    ? { ...mutation, phase: 'pending' } : mutation
  if ((pending.phase !== 'pending' && pending.error !== 'control_commit_uncertain') || membership.freshness !== 'fresh') return pending
  const confirmed = pending.targetMeshId
    ? membership.kind === 'mesh' && membership.meshId === pending.targetMeshId && membership.ready === true
    : membership.kind === 'standalone' && membership.ready === true
  return confirmed ? { ...pending, phase: 'confirmed', error: undefined } : pending
}

export function memberOperationalState(
  membership: MembershipProjection,
  mutation: MembershipMutation | undefined,
  desired?: ManagedFleetNodeReadModel,
): OperationalState {
  if (mutation?.phase === 'failed' && !membershipTransitionPending(mutation.error)) return 'attention'
  const error = desired?.lastError ?? membership.node?.lastError
  if (error && !membershipTransitionPending(error)) return 'attention'
  if (mutation?.phase === 'pending' || membershipTransitionPending(mutation?.error)) return 'applying'
  if (membership.freshness !== 'fresh' || membership.kind === 'unknown') return 'unavailable'
  if (membershipTransitionPending(error)) return 'applying'
  if (desired && (desired.state === 'detached' ? membership.kind === 'mesh'
    : (desired.meshId ?? '') !== (membership.meshId ?? ''))) return 'applying'
  if (!membership.ready) return 'applying'
  return 'current'
}

/** Membership comes from the participant. An authority row can still be unapplied intent. */
export function observedMembership(
  observed: Pick<ControlObservation, 'control' | 'freshness'> | undefined,
  authority?: Pick<ControlObservation, 'control' | 'freshness'>,
): MembershipProjection {
  const control = observed?.control
  const freshness = observed?.freshness ?? 'unknown'
  if (!control) return { kind: 'unknown', freshness }
  const node = control.nodes.find((item) => item.nodeId === control.nodeId)
  if (!control.managed || isAutonomous(control) || node?.state === 'detached') {
    return { kind: 'standalone', node, freshness,
      ready: freshness === 'fresh' && control.nativeAccessTrust?.ready === true && control.nativeAccessTrust.state === 'autonomous' }
  }
  if (!node) return { kind: 'unknown', freshness }
  if (!node.meshId) return { kind: 'standalone', node, freshness,
    ready: freshness === 'fresh' && control.nativeAccessTrust?.ready === true && control.nativeAccessTrust.state === 'autonomous' }
  return { kind: 'mesh', meshId: node.meshId, node, freshness,
    ready: freshness === 'fresh' && authority?.freshness === 'fresh'
      && membershipReady(control, authority.control, node.nodeId, node.meshId) }
}

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
  if (!local || !authority || local.nodeId !== nodeId
    || local.controlNodeId !== authority.controlNodeId) return false
  const entry = authority.nodes.find((node) => node.nodeId === authority.nodeId)
  const primary = authority.nodes.find((node) => node.nodeId === authority.controlNodeId)
  if (entry?.state !== 'active' || entry.meshId !== meshId
    || primary?.state !== 'active' || primary.meshId !== meshId) return false
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
