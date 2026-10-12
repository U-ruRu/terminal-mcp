import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { useAccessMeshState } from './accessMeshState'
import { fleetInstance, nativeDefaults, nativeSlot } from './testFixtures'
import type { AccessMeshDefaultsReadModel, AccessMeshSlotReadModel } from '../api/models'

afterEach(cleanup)

test('readable slots are visible while the independent defaults request is still pending', async () => {
  let finishDefaults: (value: AccessMeshDefaultsReadModel) => void = () => undefined
  const loadSlots = vi.fn(async () => [nativeSlot()])
  const loadDefaults = vi.fn(() => new Promise<AccessMeshDefaultsReadModel>((resolve) => { finishDefaults = resolve }))
  const instance = fleetInstance()
  const { result } = renderHook(() => useAccessMeshState([instance], loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states[instance.profile.instanceId]?.slots).toHaveLength(1))
  expect(result.current.states[instance.profile.instanceId]?.defaults).toBeUndefined()
  const slotObservedAt = result.current.states[instance.profile.instanceId]?.observedAt
  await act(async () => { finishDefaults(nativeDefaults()) })
  expect(result.current.states[instance.profile.instanceId]?.defaults).toEqual(nativeDefaults())
  expect(result.current.states[instance.profile.instanceId]?.observedAt).toBe(slotObservedAt)
})

test('failed slot refresh keeps its original observation timestamp and labels retained data as failed', async () => {
  const loadSlots = vi.fn<() => Promise<AccessMeshSlotReadModel[]>>().mockResolvedValueOnce([nativeSlot()]).mockRejectedValueOnce(new Error('request_timeout'))
  const loadDefaults = vi.fn(async () => nativeDefaults())
  const instance = fleetInstance()
  const { result } = renderHook(() => useAccessMeshState([instance], loadSlots, loadDefaults))
  await waitFor(() => expect(result.current.states[instance.profile.instanceId]?.slots).toHaveLength(1))
  const observedAt = result.current.states[instance.profile.instanceId]?.observedAt
  await act(async () => { await result.current.refresh(instance.profile.instanceId) })
  expect(result.current.states[instance.profile.instanceId]?.slots).toHaveLength(1)
  expect(result.current.states[instance.profile.instanceId]?.slotsError?.code).toBe('request_timeout')
  expect(result.current.states[instance.profile.instanceId]?.observedAt).toBe(observedAt)
})
