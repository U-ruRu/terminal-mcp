import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import { ConsoleOperationError } from '../api/slotContracts'
import type { AccessMeshSlotReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { slotReadFailure, useAccessMeshState } from './accessMeshState'

const slot: AccessMeshSlotReadModel = {
  issuerNodeId: 'issuer', slotId: 'as_one', logicalAgentId: 'la_one', publicName: 'Alpha', mode: 'persistent', state: 'active', revision: 4,
  policy: { durationSeconds: 10800, cooldownSeconds: 0, rearmEnabled: true, warningSeconds: 180, drainingSeconds: 60, releaseOnEnd: null },
  sessionLifecycle: { state: 'idle', startedAt: null, hardExpiresAt: null, rearmAt: null, remainingSeconds: 0 },
}

afterEach(cleanup)

test('support, disabled, permission and network failures have different states and safe codes', () => {
  expect(slotReadFailure(new ConsoleHttpError(404, 'not_found')).kind).toBe('unsupported')
  expect(slotReadFailure(new ConsoleOperationError('access_mesh_disabled')).kind).toBe('disabled')
  expect(slotReadFailure(new ConsoleHttpError(403, 'insufficient_scope')).kind).toBe('denied')
  expect(slotReadFailure(new Error('network response contains private data'))).toEqual({ kind: 'failed', code: 'request_failed' })
})

test('denied defaults do not discard independently readable native slots', async () => {
  const fleet: FleetInstanceView[] = [{
    profile: { instanceId: 'one', origin: 'https://one.example', displayName: 'One', credentialRef: 'cred', metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
    runtime: { instanceId: 'one', status: 'live', authStatus: 'connected', realtime: null, reconnectAttempt: 0 },
  }]
  const loadSlots = vi.fn(async () => [slot])
  const loadDefaults = vi.fn(async () => { throw new ConsoleHttpError(403, 'insufficient_scope') })
  const { result, rerender } = renderHook(() => useAccessMeshState(fleet, loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states.one?.slots).toEqual([slot]))
  expect(result.current.states.one?.defaultsError?.kind).toBe('denied')
  rerender()
  expect(loadSlots).toHaveBeenCalledTimes(1)
})
