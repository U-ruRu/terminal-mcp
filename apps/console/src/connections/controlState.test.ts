import { beforeEach, describe, expect, test } from 'vitest'

import type { ManagedFleetControlReadModel } from '../api/models'
import {
  FLEET_CONTROL_CACHE_KEY,
  loadCachedFleetControl,
  loadCachedFleetControlForProfile,
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
  test('does not propagate one control authority over another authority in the same fleet', () => {
    const firstbyteAuthority = {
      ...control('firstbyte'),
      controlNodeId: 'firstbyte',
      mesh: { meshId: 'mesh-b', displayName: 'Staging', adopted: true, updatedAt: '2026-10-02T06:00:00Z' },
      meshes: [{ meshId: 'mesh-b', displayName: 'Staging', adopted: true, updatedAt: '2026-10-02T06:00:00Z' }],
    }
    saveCachedFleetControl('main-profile', control('main'), 100)
    saveCachedFleetControl('firstbyte-profile', firstbyteAuthority, 100)

    propagateCachedFleetControl(control('main', true), 200)

    expect(loadCachedFleetControl('firstbyte-profile')?.control.controlNodeId).toBe('firstbyte')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.mesh?.meshId).toBe('mesh-b')
    expect(loadCachedFleetControl('firstbyte-profile')?.control.policy?.legacyAdmissionEnabled).toBe(false)
    expect(loadCachedFleetControl('firstbyte-profile')?.observedAt).toBe(100)
  })

  test('prefers a newer authoritative standalone projection over stale mesh membership', () => {
    const member = control('main')
    const standalone = {
      ...control('firstbyte'),
      controlNodeId: 'firstbyte',
      nodes: control('firstbyte').nodes.map((node) => node.nodeId === 'firstbyte' ? { ...node, meshId: undefined } : node),
      mesh: undefined,
    }
    saveCachedFleetControl('member-authority', member, 100)
    saveCachedFleetControl('standalone-authority', standalone, 200)

    const projected = loadCachedFleetControlForProfile('missing-profile', 'https://firstbyte.example')
    expect(projected?.control.controlNodeId).toBe('firstbyte')
    expect(projected?.control.mesh).toBeUndefined()
    expect(projected?.control.nodes.find((node) => node.nodeId === 'firstbyte')?.meshId).toBeUndefined()
    expect(projected?.observedAt).toBe(200)
  })

  test('uses the newest standalone authority instead of treating standalone as ambiguous', () => {
    const mainStandalone = {
      ...control('main'),
      nodes: control('main').nodes.map((node) => node.nodeId === 'firstbyte' ? { ...node, meshId: undefined } : node),
    }
    const firstbyteStandalone = {
      ...mainStandalone,
      nodeId: 'firstbyte',
      controlNodeId: 'firstbyte',
    }
    saveCachedFleetControl('main-authority', mainStandalone, 100)
    saveCachedFleetControl('firstbyte-authority', firstbyteStandalone, 200)

    const projected = loadCachedFleetControlForProfile('missing-profile', 'https://firstbyte.example')
    expect(projected?.control.controlNodeId).toBe('firstbyte')
    expect(projected?.control.mesh).toBeUndefined()
    expect(projected?.observedAt).toBe(200)
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

  test('projects a managed peer cache onto a profile by authoritative node origin', () => {
    saveCachedFleetControl('main-profile', control('main', true), 300)

    const projected = loadCachedFleetControlForProfile('firstbyte-profile', 'https://firstbyte.example/')

    expect(projected?.control.nodeId).toBe('firstbyte')
    expect(projected?.control.mesh?.meshId).toBe('mesh-prod')
    expect(projected?.control.policy?.legacyAdmissionEnabled).toBe(true)
    expect(projected?.observedAt).toBe(300)
  })

})
