import { describe,expect,it,vi } from 'vitest'
import { ConsoleClient } from './client'
import { ConsoleContractError } from './contracts'
import { decodeAccessMeshDefaults,decodeAccessMeshSlot,decodePolicyInheritance } from './slotContracts'

export function accessFixture() {
  return {
    ok: true, slot: {
      issuer_node_id: 'test-node', slot_id: 'as_test', public_name: 'Test-1', mode: 'persistent', state: 'active', revision: 3,
      policy: { duration_seconds: 10800, cooldown_seconds: 0, rearm_enabled: true, warning_seconds: 180, draining_seconds: 90, release_on_end: null },
      session_lifecycle: { state: 'idle', started_at: null, hard_expires_at: null, remaining_seconds: 0, rearm_at: null },
    },
  }
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

describe('current Access Mesh contracts', () => {
  it('decodes a slot without agent identity and omits any accidental code from list models', () => {
    const fixture = accessFixture()
    const slot = decodeAccessMeshSlot({ ...fixture, slot: { ...fixture.slot, access_code: 'TEST_ONLY' } })
    expect(slot.slotId).toBe('as_test')
    expect(slot).not.toHaveProperty('logicalAgentId')
    expect(slot.issuerNodeId).toBe('test-node')
    expect(JSON.stringify(slot)).not.toContain('TEST_ONLY')
  })

  it('accepts temporary Access slots and rejects the retired kind', () => {
    const fixture = accessFixture()
    expect(decodeAccessMeshSlot({ ...fixture, slot: { ...fixture.slot, mode: 'temporary' } }).mode).toBe('temporary')
    expect(() => decodeAccessMeshSlot({ ...fixture, slot: { ...fixture.slot, mode: 'legacy' } })).toThrow(ConsoleContractError)
  })

  it('rejects missing booleans rather than silently enabling rearm', () => {
    const fixture = accessFixture()
    delete (fixture.slot.policy as Record<string, unknown>).rearm_enabled
    expect(() => decodeAccessMeshSlot(fixture)).toThrow(ConsoleContractError)
  })

  it.each([true, false])('decodes Legacy admission %s from the shared defaults policy', (enabled) => {
    expect(decodeAccessMeshDefaults({ ok: true, revision: 2, policy: accessFixture().slot.policy, legacy_admission_enabled: enabled }).legacyAdmissionEnabled).toBe(enabled)
  })
})

describe('slot API transport', () => {
  it('uses durable Access Mesh idempotency key unchanged and never auto-retries a network failure', async () => {
    const fetcher = vi.fn(async () => { throw new TypeError('offline') })
    const client = new ConsoleClient('https://test.example', () => 'test-token', fetcher)
    await expect(client.accessMeshMutation({ action: 'rotate', slot_id: 'as_test', expected_revision: 3, idempotency_key: 'stable-test-key' })).rejects.toMatchObject({ code: 'network_error', status: 0 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher).toHaveBeenCalledWith(new URL('https://test.example/actions/access/mutate'), expect.objectContaining({ body: JSON.stringify({ action: 'rotate', slot_id: 'as_test', expected_revision: 3, idempotency_key: 'stable-test-key' }) }))
  })

  it('collects every page using the opaque cursor supplied by the server', async () => {
    const slot = accessFixture().slot
    const fetcher = vi.fn().mockResolvedValueOnce(response({ ok: true, slots: [slot], next_cursor: 'opaque+token' })).mockResolvedValueOnce(response({ ok: true, slots: [{ ...slot, slot_id: 'as_other' }], next_cursor: null }))
    const client = new ConsoleClient('https://test.example', () => 'test-token', fetcher)
    expect((await client.accessMeshAllSlots()).map((item) => item.slotId)).toEqual(['as_test', 'as_other'])
    expect(String(fetcher.mock.calls[1][0])).toContain('cursor=opaque%2Btoken')
  })
})

describe('native snapshot bridge and inheritance metadata', () => {
  it('keeps offline last-known parent evidence distinct from authoritative convergence', () => {
    const value = { mode: 'inherit', source: 'mesh', parent: { source: 'mesh', id: 'mesh_synthetic', revision: 7, version: 'mesh:7', freshness: 'last_known', convergence: 'pending' }, reset_supported: true, applies_to: 'next_window', pending: true }
    expect(decodePolicyInheritance(value)).toMatchObject({ mode: 'inherit', resetSupported: true, appliesTo: 'next_window', parent: { freshness: 'last_known', convergence: 'pending', revision: 7 } })
  })
  it('never treats a policy revision as a code generation', () => {
    const fixture = accessFixture()
    const result = decodeAccessMeshSlot({ ...fixture, slot: { ...fixture.slot, revision: 9, code_generation: 2, code_expires_at: null } })
    expect(result.revision).toBe(9)
    expect(result.codeGeneration).toBe(2)
    expect(result.codeExpiresAt).toBeNull()
  })
})
