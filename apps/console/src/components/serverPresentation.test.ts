import { expect, test } from 'vitest'

import { fixtureFleetModel } from '../fixtures/fleet'
import {
  needsAttention,
  resourceDisplayValue,
  resourcePercent,
  resourceVisualState,
  serverProblemSort,
  serverVisualState,
} from './serverPresentation'

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

  expect(resourceVisualState(server, 'cpu')).toBe('attention')
  expect(resourceVisualState(server, 'memory')).toBe('normal')
  expect(resourceVisualState(server, 'filesystem')).toBe('normal')
  expect(serverVisualState(server)).toBe('attention')
  expect(needsAttention(server)).toBe(true)
})

test('treats RAM and disk at 80 percent as attention', () => {
  const server = {
    ...healthy,
    resources: {
      ...healthy.resources!,
      memory: { ...healthy.resources!.memory, usedPercent: 80 },
      filesystem: { ...healthy.resources!.filesystem, usedPercent: 80 },
    },
  }

  expect(resourceVisualState(server, 'memory')).toBe('attention')
  expect(resourceVisualState(server, 'filesystem')).toBe('attention')
})

test('offline state hides last-known resource values', () => {
  const server = { ...healthy, connectivity: 'offline' as const, freshness: 'offline' as const }

  expect(resourceDisplayValue(server, 'cpu', 'Unavailable', 'load')).toBe('Unavailable')
  expect(resourceDisplayValue(server, 'memory', 'Unavailable', 'load')).toBe('Unavailable')
  expect(resourceVisualState(server, 'filesystem')).toBe('unavailable')
  expect(serverVisualState(server)).toBe('offline')
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

  expect(resourcePercent(server, 'cpu')).toBeCloseTo(110)
  expect(resourceDisplayValue(server, 'cpu', 'Unavailable', 'Load')).toBe('110%')
  expect(resourceVisualState(server, 'cpu')).toBe('attention')
})

test('blocked tasks and communication counts do not change server health', () => {
  const server = {
    ...healthy,
    blockerCount: 7,
    communication: { unread: 12, replyRequired: 4, alerts: 3 },
  }

  expect(serverVisualState(server)).toBe('healthy')
  expect(needsAttention(server)).toBe(false)
})
