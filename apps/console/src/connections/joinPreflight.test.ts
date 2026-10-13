import { expect, test } from 'vitest'
import { meshControl, standaloneControl } from '../slots/testFixtures'
import { joinPreflightError } from './joinPreflight'

const authority = meshControl('alpha')

test('same-fleet compatible autonomous node may be joined', () => {
  const standalone = { ...standaloneControl('gamma'), fleetId: authority.fleetId }
  expect(joinPreflightError(standalone, authority)).toBeNull()
})

test('different namespace needs a capable target, not a matching public identifier', () => {
  expect(joinPreflightError({ ...standaloneControl('gamma'), namespaceTransitionSupported: true }, authority)).toBeNull()
  expect(joinPreflightError(standaloneControl('gamma'), authority)).toBe('namespace_transition_unsupported')
  expect(joinPreflightError({ ...standaloneControl('gamma'), fleetId: authority.fleetId, schemaVersion: 2 }, authority)).toBe('management_contract_mismatch')
})

test('self controlled standalone remains autonomous despite historical managed flag', () => {
  const selfManaged = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'gamma', mesh: undefined, meshes: [], nodes: [] }
  expect(joinPreflightError(selfManaged, authority)).toBeNull()
  const controllerWithMembers = { ...selfManaged, nodes: meshControl('gamma').nodes }
  expect(joinPreflightError(controllerWithMembers, authority)).toBe('already_managed_by_other_authority')
})

test('foreign managed follower requires coordinated transfer despite shared fleet', () => {
  const foreign = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'beta' }
  expect(joinPreflightError(foreign, authority)).toBe('already_managed_by_other_authority')
})

test('same authority managed follower remains eligible for topology update', () => {
  const follower = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'alpha' }
  expect(joinPreflightError(follower, authority)).toBeNull()
})
