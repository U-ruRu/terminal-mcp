import { cleanup,renderHook,waitFor } from '@testing-library/react'
import { afterEach,expect,test,vi } from 'vitest'
import type { AccessMeshDefaultsReadModel } from '../api/models'
import { useAccessMeshState } from './accessMeshState'
import { fleetInstance,nativeSlot } from './testFixtures'

afterEach(cleanup)

test('slots become readable while the defaults request remains pending', async () => {
  const loadSlots = vi.fn(async () => [nativeSlot()])
  const loadDefaults = vi.fn(() => new Promise<AccessMeshDefaultsReadModel>(() => undefined))
  const instance = fleetInstance()

  const targets = [{ instance, registryScope: 'server:alpha', available: true }]
  const { result } = renderHook(() => useAccessMeshState(targets, loadSlots, loadDefaults))

  await waitFor(() => expect(result.current.states[instance.profile.instanceId]?.slots).toHaveLength(1))
  expect(result.current.states[instance.profile.instanceId]?.defaults).toBeUndefined()
})
