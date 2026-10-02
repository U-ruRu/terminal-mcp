import { beforeEach, describe, expect, test } from 'vitest'

import type { ManagedFleetControlReadModel } from '../api/models'
import {
  FLEET_CONTROL_CACHE_KEY,
  loadCachedFleetControl,
  propagateCachedFleetControl,
  saveCachedFleetControl,
} from './controlState'

const control = (nodeId: string, legacy = false): ManagedFleetControlReadModel => ({
  schemaVersion: 3,
  fleetId: 'fleet-a',
  nodeId,
  controlNodeId: 'main',
  managed: true,
  mesh: { meshId: 'mesh-prod', displayName: 'Production', adopted: true, updatedAt: '2026-10-02T06:00:00Z' },
  meshes: [{ meshId: 'mesh-prod', displayName: 'Production', adopted: true, updatedAt: '2026-10-02T06:00:00Z' }],
  nodes: ['main', 'firstbyte'].map((id) => ({
    nodeId: id,
    origin: `https://${id}.example`,
    meshId: 'mesh-prod',
    state: 'active' as const,
    desiredTopologyRevision: 2,
    appliedTopologyRevision: 2,
    desiredTrustRevision: 2,
    appliedTrustRevision: 2,
    desiredPolicyRevision: 4,
    appliedPolicyRevision: 4,
    updatedAt: '2026-10-02T06:00:00Z',
  })),
  policy: {
    durationSeconds: 1380,
    warningAfterSeconds: 1200,
    alertAfterSeconds: 1320,
    rearmAfterSeconds: 180,
    legacyAdmissionEnabled: legacy,
    revision: legacy ? 5 : 4,
    updatedAt: '2026-10-02T06:00:00Z',
  },
  revisions: { routing: 1, topology: 2, trust: 2, accessPolicy: legacy ? 5 : 4 },
  updatedAt: '2026-10-02T06:00:00Z',
})

describe('fleet control cache', () => {
  beforeEach(() => localStorage.removeItem(FLEET_CONTROL_CACHE_KEY))

  test('restores last authoritative control observation', () => {
    saveCachedFleetControl('main-profile', control('main'), 123)
    expect(loadCachedFleetControl('main-profile')).toEqual({ control: control('main'), observedAt: 123 })
  })

  test('propagates fleet-wide policy while preserving each local node identity', () => {
    saveCachedFleetControl('main-profile', control('main'), 100)
    saveCachedFleetControl('firstbyte-profile', control('firstbyte'), 100)

    propagateCachedFleetControl(control('main', true), 200)

    expect(loadCachedFleetControl('main-profile')?.control.nodeId).toBe('main')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.nodeId).toBe('firstbyte')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.policy?.legacyAdmissionEnabled).toBe(true)
    expect(loadCachedFleetControl('firstbyte-profile')?.observedAt).toBe(200)
  })
})
