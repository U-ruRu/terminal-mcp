import { expect, test } from 'vitest'
import { meshControl, standaloneControl } from '../slots/testFixtures'
import { joinPreflightError } from './joinPreflight'

const authority = meshControl('alpha')

test('same-fleet compatible autonomous node may be joined', () => {
  const standalone = { ...standaloneControl('gamma'), fleetId: authority.fleetId }
  expect(joinPreflightError(standalone, authority)).toBeNull()
})

test('reject cross-fleet or contract mismatch before enrollment or any topology write', () => {
  expect(joinPreflightError(standaloneControl('gamma'), authority)).toBe('fleet_id_mismatch')
  expect(joinPreflightError({ ...standaloneControl('gamma'), fleetId: authority.fleetId, schemaVersion: 2 }, authority)).toBe('management_contract_mismatch')
})

test('foreign managed controller is not safe to rehome even with no own mesh membership', () => {
  const selfManaged = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'gamma', mesh: undefined, meshes: [], nodes: [] }
  expect(joinPreflightError(selfManaged, authority)).toBe('already_managed_by_other_authority')
})

test('foreign managed follower requires coordinated transfer despite shared fleet', () => {
  const foreign = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'beta' }
  expect(joinPreflightError(foreign, authority)).toBe('already_managed_by_other_authority')
})

test('same authority managed follower remains eligible for topology update', () => {
  const follower = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'alpha' }
  expect(joinPreflightError(follower, authority)).toBeNull()
})
