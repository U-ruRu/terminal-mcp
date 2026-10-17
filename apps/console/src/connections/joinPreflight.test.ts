import { expect,test } from 'vitest'
import { meshControl,standaloneControl } from '../slots/testFixtures'
import { joinPreflightError } from './joinPreflight'

const authority = meshControl('alpha')

test('same-fleet compatible autonomous node may be joined', () => {
  const standalone = { ...standaloneControl('gamma'), fleetId: authority.fleetId }
  expect(joinPreflightError(standalone, authority)).toBeNull()
})

test('foreign managed controller is not safe to rehome even with no own mesh membership', () => {
  const selfManaged = { ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'gamma', mesh: undefined, meshes: [], nodes: [] }
  expect(joinPreflightError(selfManaged, authority)).toBe('already_managed_by_other_authority')
})
