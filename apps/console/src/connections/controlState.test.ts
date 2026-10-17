import { beforeEach,describe,expect,test } from 'vitest'

import type { ManagedFleetControlReadModel } from '../api/models'
import {
FLEET_CONTROL_CACHE_KEY,
isFleetControlRevisionRegression,
loadCachedFleetControl,
loadCachedFleetControlForProfile,
propagateCachedFleetControl,
saveCachedFleetControl,
} from './controlState'

const control = (nodeId: string, revision = 4): ManagedFleetControlReadModel => ({
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
    revision,
    updatedAt: '2026-10-02T06:00:00Z',
  },
  revisions: { routing: 1, topology: 2, trust: 2, accessPolicy: revision },
  updatedAt: '2026-10-02T06:00:00Z',
})

describe('fleet control cache', () => {
  beforeEach(() => localStorage.removeItem(FLEET_CONTROL_CACHE_KEY))

  test('propagates fleet-wide policy while preserving each local node identity', () => {
    saveCachedFleetControl('main-profile', control('main'), 100)
    saveCachedFleetControl('firstbyte-profile', control('firstbyte'), 100)

    propagateCachedFleetControl(control('main', 5), 200)

    expect(loadCachedFleetControl('main-profile')?.control.nodeId).toBe('main')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.nodeId).toBe('firstbyte')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.policy?.revision).toBe(5)
    expect(loadCachedFleetControl('firstbyte-profile')?.observedAt).toBe(200)
  })
  test('does not propagate one control authority over another authority in the same fleet', () => {
    const firstbyteAuthority = {
      ...control('firstbyte'),
      controlNodeId: 'firstbyte',
      mesh: { meshId: 'mesh-b', displayName: 'Staging', adopted: true, updatedAt: '2026-10-02T06:00:00Z' },
      meshes: [{ meshId: 'mesh-b', displayName: 'Staging', adopted: true, updatedAt: '2026-10-02T06:00:00Z' }],
    }
    saveCachedFleetControl('main-profile', control('main'), 100)
    saveCachedFleetControl('firstbyte-profile', firstbyteAuthority, 100)

    propagateCachedFleetControl(control('main', 5), 200)

    expect(loadCachedFleetControl('firstbyte-profile')?.control.controlNodeId).toBe('firstbyte')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.mesh?.meshId).toBe('mesh-b')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.policy?.revision).toBe(4)
    expect(loadCachedFleetControl('firstbyte-profile')?.observedAt).toBe(100)
  })

  test('prefers a newer topology revision from the same authority even when observed earlier', () => {
    const staleMember = control('main')
    const detached = {
      ...control('main'),
      revisions: { ...control('main').revisions, topology: 3 },
      nodes: control('main').nodes.map((node) => node.nodeId === 'firstbyte'
        ? { ...node, meshId: undefined, desiredTopologyRevision: 3, appliedTopologyRevision: 3 }
        : { ...node, desiredTopologyRevision: 3, appliedTopologyRevision: 3 }),
    }
    saveCachedFleetControl('stale-member', staleMember, 300)
    saveCachedFleetControl('authority-detach', detached, 200)

    const projected = loadCachedFleetControlForProfile('missing-profile', 'https://firstbyte.example')
    expect(projected?.control.revisions.topology).toBe(3)
    expect(projected?.control.mesh).toBeUndefined()
    expect(projected?.control.nodes.find((node) => node.nodeId === 'firstbyte')?.meshId).toBeUndefined()
  })

})


test('primary transfer updates the same Mesh while stale former-primary reads cannot roll it back', () => {
  const prior = control('main')
  const transferred = { ...prior, controlNodeId: 'firstbyte', revisions: { ...prior.revisions, topology: 3 } }
  saveCachedFleetControl('main-profile', prior, 100)
  propagateCachedFleetControl(transferred, 200)
  expect(loadCachedFleetControl('main-profile')?.control.controlNodeId).toBe('firstbyte')
  expect(isFleetControlRevisionRegression(prior, transferred)).toBe(true)
})
