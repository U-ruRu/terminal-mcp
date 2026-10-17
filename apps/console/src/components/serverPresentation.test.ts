import { expect,test } from 'vitest'

import { fixtureFleetModel } from '../fixtures/fleet'
import { resourcePercent,resourceVisualState,serverProblemSort,serverVisualState } from './serverPresentation'

const healthy = fixtureFleetModel.servers.find((server) => server.instanceId === 'server-c')!

test('uses approved resource thresholds and lets a resource breach outrank stale', () => {
  const server = {
    ...healthy,
    connectivity: 'stale' as const,
    freshness: 'stale' as const,
    resources: {
      ...healthy.resources!,
      cpu: { ...healthy.resources!.cpu, usagePercent: 90 },
      memory: { ...healthy.resources!.memory, usedPercent: 79.9 },
      filesystem: { ...healthy.resources!.filesystem, usedPercent: 79.9 },
    },
  }

  expect(serverVisualState(server)).toBe('attention')
})

test('treats memory at its 80 percent threshold as attention', () => {
  const server = {
    ...healthy,
    resources: {
      ...healthy.resources!,
      memory: { ...healthy.resources!.memory, usedPercent: 80 },
    },
  }

  expect(resourceVisualState(server, 'memory')).toBe('attention')
})

test('problem ordering is offline or critical, then attention, then stale, then alphabetical', () => {
  const offline = { ...healthy, instanceId: 'offline', displayName: 'Zulu', connectivity: 'offline' as const, freshness: 'offline' as const }
  const attention = {
    ...healthy,
    instanceId: 'attention',
    displayName: 'Alpha',
    resources: { ...healthy.resources!, cpu: { ...healthy.resources!.cpu, usagePercent: 91 } },
  }
  const stale = { ...healthy, instanceId: 'stale', displayName: 'Beta', connectivity: 'stale' as const, freshness: 'stale' as const }

  expect([stale, attention, offline].sort(serverProblemSort).map((server) => server.instanceId)).toEqual([
    'offline',
    'attention',
    'stale',
  ])
})

test('normalizes load average by logical CPU count when direct CPU usage is absent', () => {
  const server = {
    ...healthy,
    resources: {
      ...healthy.resources!,
      cpu: { ...healthy.resources!.cpu, usagePercent: undefined, logicalCores: 4, load1m: 4.4 },
    },
  }

  expect(resourcePercent(server, 'cpu')).toBe(100)
})

test('normalizes aggregate direct CPU percentages by logical core count', () => {
  const server = {
    ...healthy,
    resources: {
      ...healthy.resources!,
      cpu: { ...healthy.resources!.cpu, usagePercent: 170, logicalCores: 2 },
    },
  }
  expect(resourcePercent(server, 'cpu')).toBe(85)
})
