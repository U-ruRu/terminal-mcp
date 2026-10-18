import { expect,test } from 'vitest'
import { meshControl,standaloneControl } from '../slots/testFixtures'
import { memberOperationalState,membershipReady,observedMembership,resolvedMembershipMutation,type MembershipProjection } from './membershipReadiness'

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

test('committed authority membership does not replace the participant standalone observation', () => {
  const authority = ready()
  authority.nodes[1].appliedTopologyRevision = 0
  const local = { ...standaloneControl('beta'), nativeAccessTrust: {
    state: 'pending', ready: false, peers: {}, missingTransport: [] as string[],
  } }

  const membership = observedMembership({ control: local, freshness: 'fresh' }, { control: authority, freshness: 'fresh' })

  expect(membership).toMatchObject({ kind: 'standalone', freshness: 'fresh', ready: false })
})

test.each([
  { label: 'fresh applied Mesh', membership: { kind: 'mesh', meshId: 'mesh-prod', ready: true, freshness: 'fresh' }, phase: 'confirmed' },
  { label: 'stale applied Mesh', membership: { kind: 'mesh', meshId: 'mesh-prod', ready: true, freshness: 'stale' }, phase: 'pending' },
  { label: 'pending native trust', membership: { kind: 'mesh', meshId: 'mesh-prod', ready: false, freshness: 'fresh' }, phase: 'pending' },
  { label: 'participant still standalone', membership: { kind: 'standalone', ready: false, freshness: 'fresh' }, phase: 'pending' },
])('transition receipt resolves from $label', ({ membership, phase }) => {
  const result = resolvedMembershipMutation(membership as MembershipProjection, {
    targetMeshId: 'mesh-prod', phase: 'failed', error: 'mesh_transition_pending',
  })

  expect(result?.phase).toBe(phase)
  expect(result?.error).toBe(phase === 'confirmed' ? undefined : 'mesh_transition_pending')
})

test.each([
  { state: 'pending', ready: false, phase: 'pending', operational: 'applying' },
  { state: 'autonomous', ready: true, phase: 'confirmed', operational: 'current' },
])('standalone $state proof determines detach confirmation', ({ state, ready: proofReady, phase, operational }) => {
  const local = { ...standaloneControl('beta'), nativeAccessTrust: {
    state, ready: proofReady, peers: {}, missingTransport: [] as string[],
  } }
  const membership = observedMembership({ control: local, freshness: 'fresh' })

  const result = resolvedMembershipMutation(membership, { targetMeshId: '', phase: 'pending', error: 'mesh_transition_pending' })

  expect(result?.phase).toBe(phase)
  expect(memberOperationalState(membership, result)).toBe(operational)
})

test.each([
  { error: 'mesh_transition_pending', expected: 'applying' },
  { error: 'replication_failed', expected: 'attention' },
])('replication $error has $expected presentation', ({ error, expected }) => {
  const membership: MembershipProjection = { kind: 'standalone', ready: false, freshness: 'fresh' }
  const desired = { ...ready().nodes[1], lastError: error }

  expect(memberOperationalState(membership, undefined, desired)).toBe(expected)
})
