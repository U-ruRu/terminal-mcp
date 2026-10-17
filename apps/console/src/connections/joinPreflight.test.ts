import { expect, test } from 'vitest'
import { meshControl, standaloneControl } from '../slots/testFixtures'
import { joinPreflightError } from './joinPreflight'

const authority = meshControl('alpha')

test('same-fleet autonomous node may be joined', () => {
  const target = { ...standaloneControl('gamma'), fleetId: authority.fleetId }

  expect(joinPreflightError(target, authority)).toBeNull()
})

test('current autonomous node may join a different namespace', () => {
  const target = { ...standaloneControl('gamma'), namespaceTransitionSupported: true }

  expect(joinPreflightError(target, authority)).toBeNull()
})

test('paired controller may move to a different Mesh after the server validates the transition', () => {
  const target = {
    ...meshControl('gamma'), nodeId: 'gamma', controlNodeId: 'gamma',
    mesh: undefined, meshes: [], nodes: meshControl('gamma').nodes,
  }

  expect(joinPreflightError(target, authority)).toBeNull()
})
