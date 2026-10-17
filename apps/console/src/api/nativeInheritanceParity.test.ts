import { expect,test } from 'vitest'
import { nativeInheritanceFixtures as fixture } from './fixtures/androidSlotInheritance'
import { decodeAccessMeshSlot } from './slotContracts'

test('current v2 slot decoding preserves the active window policy snapshot', () => {
  const future = decodeAccessMeshSlot(fixture.future_slot)
  expect(future.policy.durationSeconds).toBe(10800)
  expect(future.windowPolicy?.durationSeconds).toBe(fixture.standalone_initial.slot.policy.duration_seconds)
  expect(future.sessionLifecycle.hardExpiresAt).toBe(fixture.standalone_initial.slot.session_lifecycle.hard_expires_at)
  expect(future.inheritanceSupported).toBe(true)
  expect(future.managementContractVersion).toBe(2)
  expect(future.inheritance).toMatchObject({ mode: 'inherit', source: 'standalone', appliesTo: 'next_window', pending: true })
})
