import { expect, test } from 'vitest'
import { ConsoleContractError } from './contracts'
import { nativeInheritanceFixtures as fixture } from './fixtures/androidSlotInheritance'
import { decodeAccessMeshDefaults, decodeAccessMeshSlot } from './slotContracts'

test('real standalone HTTP keeps the new three-hour policy separate from the active window', () => {
  const initial = decodeAccessMeshSlot(fixture.standalone_initial)
  const future = decodeAccessMeshSlot(fixture.future_slot)
  expect(future.policy.durationSeconds).toBe(10800)
  expect(future.windowPolicy).toEqual(initial.policy)
  expect(future.sessionLifecycle.hardExpiresAt).toBe(initial.sessionLifecycle.hardExpiresAt)
  expect(future.inheritanceSupported).toBe(true)
  expect(future.managementContractVersion).toBe(2)
  expect(future.inheritance).toMatchObject({ mode: 'inherit', source: 'standalone', appliesTo: 'next_window', pending: true })
})

test('real HTTP slot override and reset expose the same effective parent', () => {
  const overridden = decodeAccessMeshSlot(fixture.override_slot)
  const reset = decodeAccessMeshSlot(fixture.reset_slot)
  expect(overridden.inheritance?.mode).toBe('override')
  expect(overridden.policy.durationSeconds).toBe(7200)
  expect(overridden.inheritedPolicy?.durationSeconds).toBe(10800)
  expect(reset.inheritance?.mode).toBe('inherit')
  expect(reset.policy).toEqual(reset.inheritedPolicy)
})

test('real Mesh HTTP proves capability and its existing authority rather than assuming scope', () => {
  const mesh = decodeAccessMeshDefaults(fixture.mesh_defaults)
  const local = decodeAccessMeshDefaults(fixture.standalone_defaults)
  expect(mesh.meshPolicySupported).toBe(true)
  expect(mesh.managementContractVersion).toBe(2)
  expect(mesh.inheritance).toMatchObject({ source: 'mesh', parent: { source: 'mesh', freshness: 'authoritative' } })
  expect(local.meshPolicySupported).toBe(false)
  expect(local.inheritance?.source).toBe('standalone')
  expect(decodeAccessMeshSlot(fixture.mesh_slot).inheritance?.source).toBe('mesh')
  expect(decodeAccessMeshSlot(fixture.mesh_slot).inheritance?.parent).toMatchObject({ source: 'server', id: 'firstbyte' })
})

test.each(['inheritance', 'inherited_policy', 'policy_override', 'current_policy', 'policy_applies_to', 'policy_source'])('a server claiming inheritance cannot omit slot %s', (field) => {
  const slot = { ...fixture.future_slot.slot } as Record<string, unknown>
  delete slot[field]
  expect(() => decodeAccessMeshSlot({ ok: true, slot })).toThrow(ConsoleContractError)
})

test.each(['inheritance', 'inherited_policy', 'policy_override', 'mesh_policy_supported'])('a server claiming inheritance cannot omit defaults %s', (field) => {
  const defaults = { ...fixture.mesh_defaults } as Record<string, unknown>
  delete defaults[field]
  expect(() => decodeAccessMeshDefaults(defaults)).toThrow(ConsoleContractError)
})

test.each(['true', 1, null, {}])('capabilities require boolean flags: %s', (value) => {
  expect(() => decodeAccessMeshDefaults({ ...fixture.mesh_defaults, mesh_policy_supported: value })).toThrow(ConsoleContractError)
})


test('v2 management cannot omit generation or policy-snapshot proof', () => {
  const missingGeneration = { ...fixture.mesh_slot.slot } as Record<string, unknown>
  delete missingGeneration.code_generation
  expect(() => decodeAccessMeshSlot({ ok: true, slot: missingGeneration })).toThrow(ConsoleContractError)
  expect(() => decodeAccessMeshDefaults({ ...fixture.mesh_defaults, inheritance_supported: false })).toThrow(ConsoleContractError)
})
