import { act,cleanup,renderHook,waitFor } from '@testing-library/react'
import { afterEach,expect,test,vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import type { AccessMeshSlotReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { useAccessMeshState } from './accessMeshState'
import { fleetInstance,nativeDefaults } from './testFixtures'

const slot: AccessMeshSlotReadModel = {
  issuerNodeId: 'issuer', slotId: 'as_one', publicName: 'Alpha', mode: 'persistent', state: 'active', revision: 4,
  policy: { durationSeconds: 10800, cooldownSeconds: 0, rearmEnabled: true, warningSeconds: 180, drainingSeconds: 60, releaseOnEnd: null },
  sessionLifecycle: { state: 'idle', startedAt: null, hardExpiresAt: null, rearmAt: null, remainingSeconds: 0 },
}

afterEach(cleanup)

test('denied defaults do not discard independently readable native slots', async () => {
  const fleet: FleetInstanceView[] = [{
    profile: { instanceId: 'one', origin: 'https://one.example', displayName: 'One', credentialRef: 'cred', metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
    runtime: { instanceId: 'one', status: 'live', authStatus: 'connected', realtime: null, reconnectAttempt: 0 },
  }]
  const loadSlots = vi.fn(async () => [slot])
  const loadDefaults = vi.fn(async () => { throw new ConsoleHttpError(403, 'insufficient_scope') })
  const { result } = renderHook(() => useAccessMeshState(fleet, loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states.one?.slots).toEqual([slot]))
  expect(result.current.states.one?.defaultsError?.kind).toBe('denied')
})

test('removing a slot prevents a previously started read from restoring it', async () => {
  const fleet = [fleetInstance()]
  let finishRead: (slots: AccessMeshSlotReadModel[]) => void = () => undefined
  const loadSlots = vi.fn<() => Promise<AccessMeshSlotReadModel[]>>()
    .mockResolvedValueOnce([slot])
    .mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve }))
  const loadDefaults = vi.fn(async () => nativeDefaults())
  const { result } = renderHook(() => useAccessMeshState(fleet, loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states.alpha?.slots).toEqual([slot]))
  let pendingRead: Promise<unknown> | undefined
  await act(async () => { pendingRead = result.current.refresh('alpha') })

  act(() => result.current.removeSlot(slot.issuerNodeId, slot.slotId))
  await act(async () => { finishRead([slot]); await pendingRead })

  expect(result.current.states.alpha?.slots).toEqual([])
})
