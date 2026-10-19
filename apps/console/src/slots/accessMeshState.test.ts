import { act,cleanup,renderHook,waitFor } from '@testing-library/react'
import { afterEach,expect,test,vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import type { AccessMeshDefaultsReadModel, AccessMeshSlotReadModel } from '../api/models'
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
  const targets = fleet.map((instance) => ({ instance, registryScope: 'server:one', available: true }))
  const { result } = renderHook(() => useAccessMeshState(targets, loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states.one?.slots).toEqual([slot]))
  expect(result.current.states.one?.defaultsError?.kind).toBe('denied')
})

test('removing a slot prevents a previously started read from restoring it', async () => {
  const registryScope = 'server:alpha'
  const targets = [{ instance: fleetInstance(), registryScope, available: true }]
  let finishRead: (slots: AccessMeshSlotReadModel[]) => void = () => undefined
  const loadSlots = vi.fn<() => Promise<AccessMeshSlotReadModel[]>>()
    .mockResolvedValueOnce([slot])
    .mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve }))
  const loadDefaults = vi.fn(async () => nativeDefaults())
  const { result } = renderHook(() => useAccessMeshState(targets, loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states.alpha?.slots).toEqual([slot]))
  let pendingRead: Promise<unknown> | undefined
  await act(async () => { pendingRead = result.current.refresh('alpha') })

  act(() => result.current.removeSlot(registryScope, slot.issuerNodeId, slot.slotId))
  await act(async () => { finishRead([slot]); await pendingRead })

  expect(result.current.states.alpha?.slots).toEqual([])
})

test.each(['unrelated target', 'transient connection status'] as const)('%s preserves the existing read and its pending defaults', async (change) => {
  const alpha = { instance: fleetInstance(), registryScope: 'mesh:production', available: true }
  const beta = { instance: fleetInstance('beta'), registryScope: 'server:beta', available: true }
  let finishDefaults: (defaults: AccessMeshDefaultsReadModel) => void = () => undefined
  const pendingDefaults = new Promise<AccessMeshDefaultsReadModel>((resolve) => { finishDefaults = resolve })
  const loadSlots = vi.fn(async (id: string) => id === 'alpha' ? [slot] : [])
  const loadDefaults = vi.fn((id: string) => id === 'alpha' ? pendingDefaults : Promise.resolve(nativeDefaults()))
  const { result, rerender } = renderHook(({ targets }) => useAccessMeshState(targets, loadSlots, loadDefaults), {
    initialProps: { targets: [alpha] },
  })
  await waitFor(() => expect(result.current.states.alpha?.slots).toEqual([slot]))

  if (change === 'unrelated target') {
    rerender({ targets: [alpha, beta] })
    await waitFor(() => expect(result.current.states.beta?.slots).toEqual([]))
  } else {
    rerender({ targets: [{ ...alpha, available: false, instance: { ...alpha.instance, runtime: { ...alpha.instance.runtime, authStatus: 'restoring' } } }] })
    rerender({ targets: [alpha] })
  }

  expect(result.current.states.alpha?.slots).toEqual([slot])
  expect(loadSlots.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(1)
  expect(loadDefaults.mock.calls.filter(([id]) => id === 'alpha')).toHaveLength(1)
  const defaults = nativeDefaults()
  await act(async () => { finishDefaults(defaults); await pendingDefaults })
  await waitFor(() => expect(result.current.states.alpha?.defaults).toEqual(defaults))
})
