import { expect, test } from 'vitest'
import type { FleetInstanceView } from './types'
import { preserveSavedServerInstances } from './instanceSet'

const view = (instanceId: string, status: FleetInstanceView['runtime']['status']): FleetInstanceView => ({
  profile: { instanceId, origin: `https://${instanceId}.example`, displayName: instanceId, credentialRef: `cred-${instanceId}`, metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
  runtime: { instanceId, status, authStatus: 'connected', reconnectAttempt: 0, realtime: null },
})

test('projected runtime wins while saved servers missing from projection stay addressable', () => {
  const projected = view('alpha', 'live')
  const directAlpha = view('alpha', 'offline')
  const recoveringBeta = view('beta', 'reconnecting')
  const merged = preserveSavedServerInstances([projected], [directAlpha, recoveringBeta])
  expect(merged.map((item) => [item.profile.instanceId, item.runtime.status])).toEqual([
    ['alpha', 'live'],
    ['beta', 'reconnecting'],
  ])
})
