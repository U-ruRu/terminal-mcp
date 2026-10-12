import { expect, test } from 'vitest'
import { meshControl } from '../slots/testFixtures'
import { membershipReady, nativeTrustReady } from './membershipReadiness'

function ready(id = 'alpha') {
  return { ...meshControl(id), nativeAccessTrust: { state: 'ready', ready: true, meshId: 'mesh-prod', peers: { [id === 'alpha' ? 'beta' : 'alpha']: 'verified' }, missingTransport: [] as string[] } }
}
test('Join requires fresh callers to supply both own applied state and native proof readiness', () => {
  expect(membershipReady(ready('beta'), ready(), 'beta', 'mesh-prod')).toBe(true)
  expect(membershipReady(meshControl('beta'), ready(), 'beta', 'mesh-prod')).toBe(false)
  expect(membershipReady(ready(), ready(), 'beta', 'mesh-prod')).toBe(false)
  const pending = ready('beta'); pending.nativeAccessTrust.ready = false
  expect(membershipReady(pending, ready(), 'beta', 'mesh-prod')).toBe(false)
  pending.nativeAccessTrust.ready = true; pending.nativeAccessTrust.peers.alpha = 'enrollment_pending'
  expect(membershipReady(pending, ready(), 'beta', 'mesh-prod')).toBe(false)
})
test('matching local revisions cannot confirm a newer desired authority revision', () => {
  const authority = ready()
  authority.nodes[1].desiredTopologyRevision = 2
  authority.nodes[1].appliedTopologyRevision = 2
  expect(membershipReady(ready('beta'), authority, 'beta', 'mesh-prod')).toBe(false)
  const local = ready('beta'); local.nodes[1].lastError = 'apply_pending'
  expect(membershipReady(local, ready(), 'beta', 'mesh-prod')).toBe(false)
})
test('foreign authority, detached node and stale trust identity fail closed', () => {
  const local = ready('beta'); local.controlNodeId = 'other'
  expect(membershipReady(local, ready(), 'beta', 'mesh-prod')).toBe(false)
  const detached = ready('beta'); detached.nodes[1].state = 'detached'
  expect(membershipReady(detached, ready(), 'beta', 'mesh-prod')).toBe(false)
  expect(nativeTrustReady(ready(), 'other-mesh')).toBe(false)
  const autonomous = ready(); autonomous.nativeAccessTrust.state = 'autonomous'
  expect(nativeTrustReady(autonomous, 'mesh-prod')).toBe(false)
  const missing = ready(); missing.nativeAccessTrust.missingTransport = ['beta']
  expect(nativeTrustReady(missing, 'mesh-prod')).toBe(false)
})
