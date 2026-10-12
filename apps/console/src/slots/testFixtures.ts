import type { AccessMeshDefaultsReadModel, AccessMeshSlotReadModel, ManagedFleetControlReadModel, SlotPolicyInheritanceReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'

export function nativeInheritance(mode: 'inherit' | 'override' | 'stored' = 'inherit', source: 'standalone' | 'mesh' = 'standalone'): SlotPolicyInheritanceReadModel {
  return {
    mode, source: mode === 'inherit' ? source : 'slot',
    parent: { source, id: source === 'mesh' ? 'mesh-prod' : 'alpha', revision: 2, version: 'parent-v2', freshness: 'authoritative', convergence: 'converged' },
    resetSupported: true, appliesTo: 'next_window', pending: false,
  }
}
export function nativeSlot(id = 'alpha'): AccessMeshSlotReadModel {
  return {
    issuerNodeId: id, slotId: 'as_' + id, logicalAgentId: 'la_' + id, publicName: id === 'alpha' ? 'Alpha' : id === 'beta' ? 'Beta' : id,
    inheritanceSupported: true, mode: 'persistent', state: 'active', revision: 7, codeGeneration: 2, codeExpiresAt: null,
    createdAt: '2026-10-12T09:00:00Z', updatedAt: '2026-10-12T10:00:00Z',
    policy: { durationSeconds: 1380, warningSeconds: 180, drainingSeconds: 90, cooldownSeconds: 180, rearmEnabled: true, releaseOnEnd: null },
    inheritance: nativeInheritance(), windowPolicy: undefined,
    sessionLifecycle: { state: 'idle', startedAt: null, hardExpiresAt: null, rearmAt: null, remainingSeconds: 0 },
  }
}
export function nativeDefaults(): AccessMeshDefaultsReadModel {
  const inheritance = nativeInheritance(); inheritance.parent = null; inheritance.resetSupported = false
  return { inheritanceSupported: true, meshPolicySupported: false, revision: 2, legacyEnabled: false, policy: nativeSlot().policy, inheritance, writeTarget: 'access_defaults' }
}
export function fleetInstance(id = 'alpha'): FleetInstanceView {
  return {
    profile: { instanceId: id, origin: `https://${id}.example`, displayName: id === 'alpha' ? 'Alpha server' : id === 'beta' ? 'Beta server' : id, credentialRef: 'cred-' + id,
      metadata: { deviceId: 'device-' + id, clientId: 'console', deviceLabel: 'Console', scope: 'terminal:read terminal:execute', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
    runtime: { instanceId: id, status: 'live', authStatus: 'connected', reconnectAttempt: 0, realtime: null },
  }
}
export function standaloneControl(id = 'alpha'): ManagedFleetControlReadModel {
  return { schemaVersion: 3, fleetId: id, nodeId: id, controlNodeId: id, managed: false, meshes: [], nodes: [], revisions: { routing: 0, topology: 0, trust: 0, accessPolicy: 0 }, updatedAt: '2026-10-12T10:00:00Z' }
}
export function meshControl(nodeId = 'alpha'): ManagedFleetControlReadModel {
  const mesh = { meshId: 'mesh-prod', displayName: 'Production', adopted: true, adoptedAt: '2026-10-12T09:00:00Z', updatedAt: '2026-10-12T10:00:00Z' }
  return { schemaVersion: 3, fleetId: 'fleet', nodeId, controlNodeId: 'alpha', managed: true, mesh, meshes: [mesh],
    nodes: ['alpha', 'beta'].map((id) => ({ nodeId: id, origin: `https://${id}.example`, meshId: 'mesh-prod', state: 'active', desiredTopologyRevision: 1, appliedTopologyRevision: 1, desiredTrustRevision: 1, appliedTrustRevision: 1, desiredPolicyRevision: 1, appliedPolicyRevision: 1, updatedAt: '2026-10-12T10:00:00Z' })),
    revisions: { routing: 1, topology: 1, trust: 1, accessPolicy: 1 }, updatedAt: '2026-10-12T10:00:00Z' }
}
