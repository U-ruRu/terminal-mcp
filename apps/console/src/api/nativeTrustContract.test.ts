import { expect, test } from 'vitest'
import { decodeManagedFleetControl } from './contracts'

const control = { schema_version: 3, fleet_id: 'fleet', node_id: 'beta', control_node_id: 'alpha', managed: true, meshes: [], nodes: [], revisions: { routing: 1, topology: 1, trust: 1, access_policy: 1 }, updated_at: '2026-10-12T15:00:00Z' }
test('decodes native trust independently of topology revisions', () => {
  const result = decodeManagedFleetControl({ control: { ...control, native_access_trust: { state: 'pending', ready: false, mesh_id: 'mesh-prod', peers: { alpha: 'enrollment_pending' }, missing_transport: ['alpha'] } } })
  expect(result.nativeAccessTrust).toEqual({ state: 'pending', ready: false, meshId: 'mesh-prod', peers: { alpha: 'enrollment_pending' }, missingTransport: ['alpha'] })
  expect(decodeManagedFleetControl({ control }).nativeAccessTrust).toBeUndefined()
})
test('rejects malformed readiness and accepts autonomous status without transport list', () => {
  expect(() => decodeManagedFleetControl({ control: { ...control, native_access_trust: { state: 'ready', ready: 'true', peers: {} } } })).toThrow()
  expect(decodeManagedFleetControl({ control: { ...control, native_access_trust: { state: 'autonomous', ready: true, peers: {} } } }).nativeAccessTrust?.missingTransport).toEqual([])
})
