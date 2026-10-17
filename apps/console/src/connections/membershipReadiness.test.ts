import { expect,test } from 'vitest'
import { meshControl } from '../slots/testFixtures'
import { membershipReady } from './membershipReadiness'

function ready(id = 'alpha') {
  return { ...meshControl(id), nativeAccessTrust: { state: 'ready', ready: true, meshId: 'mesh-prod', peers: { [id === 'alpha' ? 'beta' : 'alpha']: 'verified' }, missingTransport: [] as string[] } }
}
test('matching applied state and verified native proof confirm membership', () => {
  expect(membershipReady(ready('beta'), ready(), 'beta', 'mesh-prod')).toBe(true)
})
test('matching local revisions cannot confirm a newer desired authority revision', () => {
  const authority = ready()
  authority.nodes[1].desiredTopologyRevision = 2
  authority.nodes[1].appliedTopologyRevision = 2
  expect(membershipReady(ready('beta'), authority, 'beta', 'mesh-prod')).toBe(false)
})

test('a paired follower confirms the same Mesh after fresh applied-state reads', () => {
  const follower = { ...ready('beta'), fleetId: 'retained-transport-label' }
  expect(membershipReady(ready(), follower, 'alpha', 'mesh-prod')).toBe(true)
  expect(membershipReady(ready(), follower, 'alpha', 'another-mesh')).toBe(false)
})
