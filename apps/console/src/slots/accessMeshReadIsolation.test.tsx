import { act,cleanup,renderHook } from '@testing-library/react'
import { afterEach,expect,test,vi } from 'vitest'
import type { AccessMeshDefaultsReadModel } from '../api/models'
import { useAccessMeshState } from './accessMeshState'
import { fleetInstance,nativeDefaults,nativeSlot } from './testFixtures'

afterEach(() => { cleanup(); vi.useRealTimers() })

test('background slot reads continue without restarting pending defaults', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  const initial = nativeSlot()
  const added = nativeSlot('beta')
  const loadSlots = vi.fn().mockResolvedValueOnce([initial]).mockResolvedValue([initial, added])
  let finishDefaults: (defaults: AccessMeshDefaultsReadModel) => void = () => undefined
  const pendingDefaults = new Promise<AccessMeshDefaultsReadModel>((resolve) => { finishDefaults = resolve })
  const loadDefaults = vi.fn(() => pendingDefaults)
  const instance = fleetInstance()

  const targets = [{ instance, registryScope: 'server:alpha', available: true }]
  const { result } = renderHook(() => useAccessMeshState(targets, loadSlots, loadDefaults))

  await act(async () => { await Promise.resolve() })
  expect(result.current.states[instance.profile.instanceId]?.slots).toHaveLength(1)
  expect(result.current.states[instance.profile.instanceId]?.defaults).toBeUndefined()

  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })

  expect(result.current.states[instance.profile.instanceId]?.slots).toEqual([initial, added])
  expect(result.current.states[instance.profile.instanceId]?.defaultsPending).toBe(true)
  expect(loadDefaults).toHaveBeenCalledTimes(1)
  const defaults = nativeDefaults()
  await act(async () => { finishDefaults(defaults); await pendingDefaults })
  expect(result.current.states[instance.profile.instanceId]?.defaults).toEqual(defaults)
})
