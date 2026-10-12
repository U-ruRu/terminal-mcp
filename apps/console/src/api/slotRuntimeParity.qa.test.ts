import { describe, expect, it, vi } from 'vitest'
import { ConsoleClient, ConsoleHttpError } from './client'
import { ConsoleContractError } from './contracts'
import { runtimeSlotFixtures as fixture } from './fixtures/androidSlotRuntime7013'
import {
  ConsoleOperationError, decodeAccessMeshDefaults, decodeAccessMeshSlot,
  decodeAccessMeshSlots, decodeManagedSlotStatus,
} from './slotContracts'

const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
})

/** Independent fixtures exercise actual Python HTTP output, not TS-shaped mocks. */
describe('Android slot adapters: isolated server payload parity', () => {
  it.each([
    fixture.access_persistent_initial, fixture.access_persistent_active,
    fixture.access_persistent_changed, fixture.access_legacy_initial,
  ])('decodes the actual native read projection $slot.mode / $slot.revision', (wire) => {
    const slot = decodeAccessMeshSlot(wire)
    expect(slot.slotId).toBe(wire.slot.slot_id)
    expect(slot.issuerNodeId).toBe(wire.slot.issuer_node_id)
    expect(slot.logicalAgentId).toBe(wire.slot.logical_agent_id)
    expect(slot.policy.durationSeconds).toBe(wire.slot.policy.duration_seconds)
    expect(slot.sessionLifecycle.hardExpiresAt).toBe(wire.slot.session_lifecycle.hard_expires_at)
    expect(slot.codeExpiresAt).toBeUndefined()
    expect(slot.inheritance).toBeUndefined()
  })

  it('preserves the 3-hour duration from a created and started native slot', () => {
    const slot = decodeAccessMeshSlot(fixture.access_persistent_active)
    expect(slot.policy.durationSeconds).toBe(10800)
    expect(Date.parse(slot.sessionLifecycle.hardExpiresAt!) - Date.parse(slot.sessionLifecycle.startedAt!)).toBe(10800 * 1000)
  })

  it('decodes issuer defaults and a complete collection independently', () => {
    expect(decodeAccessMeshDefaults(fixture.access_defaults).policy.durationSeconds).toBe(fixture.access_defaults.policy.duration_seconds)
    const page = decodeAccessMeshSlots(fixture.access_collection)
    expect(page.slots).toHaveLength(2)
    expect(page.nextCursor).toBeNull()
    expect(new Set(page.slots.map((slot) => slot.issuerNodeId + '/' + slot.slotId)).size).toBe(2)
  })

  it('preserves actual managed authority denial instead of an empty success', () => {
    expect(() => decodeManagedSlotStatus(fixture.managed_denial)).toThrow(new ConsoleOperationError('authority_unavailable'))
  })

  it('rejects a wrong slot from the actual successful HTTP shape', async () => {
    const fetcher = vi.fn().mockResolvedValue(response(fixture.access_persistent_active))
    const client = new ConsoleClient('https://fixture.example', () => 'synthetic-test-auth', fetcher)
    await expect(client.accessMeshSlot('another-slot')).rejects.toBeInstanceOf(ConsoleContractError)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it.each([401, 403, 404, 405, 500])('keeps HTTP %s distinguishable from missing slots without automatic retry', async (status) => {
    const fetcher = vi.fn().mockResolvedValue(response({ code: 'access_denied', message: 'private response content' }, status))
    const client = new ConsoleClient('https://fixture.example', () => 'synthetic-test-auth', fetcher)
    const error = await client.accessMeshAllSlots().catch((failure: unknown) => failure)
    expect(error).toBeInstanceOf(ConsoleHttpError)
    expect((error as ConsoleHttpError).status).toBe(status)
    expect((error as Error).message).not.toContain('private response content')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('rejects a malformed later page rather than exposing a partial collection as complete', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(response({ ok: true, slots: [fixture.access_persistent_active.slot], next_cursor: 'fixture-cursor' }))
      .mockResolvedValueOnce(response({ ok: true, next_cursor: null }))
    const client = new ConsoleClient('https://fixture.example', () => 'synthetic-test-auth', fetcher)
    await expect(client.accessMeshAllSlots()).rejects.toBeInstanceOf(ConsoleContractError)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
