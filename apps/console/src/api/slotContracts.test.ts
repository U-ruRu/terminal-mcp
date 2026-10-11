import { describe, expect, it, vi } from 'vitest'
import { ConsoleClient, ConsoleHttpError } from './client'
import { ConsoleContractError } from './contracts'
import { ConsoleOperationError, decodeAccessMeshDefaults, decodeAccessMeshSlot, decodeAccessMeshSlots, decodeManagedSlotStatus, decodeSlotMutation } from './slotContracts'

export function managedFixture() {
  return {
    ok: true, logical_agent_id: 'la_test', public_name: 'Test-1', authority_node_id: 'test-node', authority_epoch: 1,
    policy: { default_duration_seconds: 10800, warning_before_expiry_seconds: 180, draining_before_expiry_seconds: 90, rearm_after_seconds: 0, revision: 4 },
    window: {
      work_window_id: 'ww_test', state: 'active', lifecycle: 'open', opened_at: '2026-10-01T00:00:00+00:00',
      elapsed_seconds: 10, remaining_seconds: 10790, initial_duration_seconds: 10800, effective_duration_seconds: 10800,
      hard_expires_at: '2026-10-01T03:00:00+00:00', warning_before_expiry_seconds: 180, draining_before_expiry_seconds: 90,
      rearm_after_seconds: 0, rearm_at: null, window_revision: 2,
    }, session: null,
  }
}
export function accessFixture() {
  return {
    ok: true, slot: {
      issuer_node_id: 'test-node', slot_id: 'as_test', logical_agent_id: 'la_test', public_name: 'Test-1', mode: 'persistent', state: 'active', revision: 3,
      policy: { duration_seconds: 10800, cooldown_seconds: 0, rearm_enabled: true, warning_seconds: 180, draining_seconds: 90, release_on_end: null },
      session_lifecycle: { state: 'idle', started_at: null, hard_expires_at: null, remaining_seconds: 0, rearm_at: null },
    },
  }
}
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

describe('managed per-slot contracts', () => {
  it('preserves three hours, before-expiry units, zero rearm, and separate revisions', () => {
    const status = decodeManagedSlotStatus(managedFixture())
    expect(status.policy).toEqual({ defaultDurationSeconds: 10800, warningBeforeExpirySeconds: 180, drainingBeforeExpirySeconds: 90, rearmAfterSeconds: 0, revision: 4 })
    expect(status.window?.effectiveDurationSeconds).toBe(10800)
    expect(status.window?.windowRevision).toBe(2)
  })
  it('accepts no current window, but does not infer absent fields as empty', () => {
    expect(decodeManagedSlotStatus({ ...managedFixture(), window: null }).window).toBeNull()
    const incomplete: Record<string, unknown> = managedFixture()
    delete incomplete.window
    expect(() => decodeManagedSlotStatus(incomplete)).toThrow(ConsoleContractError)
  })
  it.each(['default_duration_seconds', 'warning_before_expiry_seconds', 'draining_before_expiry_seconds', 'rearm_after_seconds', 'revision'])('rejects a missing policy field %s', (key) => {
    const fixture = managedFixture()
    delete (fixture.policy as Record<string, unknown>)[key]
    expect(() => decodeManagedSlotStatus(fixture)).toThrow(ConsoleContractError)
  })
  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, true, '10800'])('rejects invalid duration %s', (value) => {
    const fixture = managedFixture()
    ;(fixture.policy as Record<string, unknown>).default_duration_seconds = value
    expect(() => decodeManagedSlotStatus(fixture)).toThrow(ConsoleContractError)
  })
  it('preserves domain denial instead of claiming no slot exists', () => {
    expect(() => decodeManagedSlotStatus({ ok: false, code: 'authority_unavailable' })).toThrow(new ConsoleOperationError('authority_unavailable'))
  })
  it('does not leak free-form server text into diagnostics', () => {
    const receipt = decodeSlotMutation({ ok: false, error: { code: 'access_denied', message: 'private detail' } })
    expect(receipt.code).toBe('access_denied')
    expect(() => decodeManagedSlotStatus({ ok: false, error: 'private detail' })).toThrow(new ConsoleOperationError('request_failed'))
  })
})

describe('Access Mesh contracts are distinct from old persistent snapshots', () => {
  it('decodes issuer/slot/logical identity separately and omits any accidental code from list models', () => {
    const fixture = accessFixture()
    const slot = decodeAccessMeshSlot({ ...fixture, slot: { ...fixture.slot, access_code: 'TEST_ONLY' } })
    expect(slot.slotId).toBe('as_test')
    expect(slot.logicalAgentId).toBe('la_test')
    expect(slot.issuerNodeId).toBe('test-node')
    expect(JSON.stringify(slot)).not.toContain('TEST_ONLY')
  })
  it('rejects partial lists and validates explicit empty lists', () => {
    expect(() => decodeAccessMeshSlots({ ok: true, next_cursor: null })).toThrow(ConsoleContractError)
    expect(decodeAccessMeshSlots({ ok: true, slots: [], next_cursor: null })).toEqual({ slots: [], nextCursor: null })
  })
  it('preserves defaults and does not invent a Mesh-wide source', () => {
    const defaults = decodeAccessMeshDefaults({ ok: true, revision: 2, policy: accessFixture().slot.policy, legacy_enabled: false })
    expect(defaults.policy.durationSeconds).toBe(10800)
    expect(defaults.legacyEnabled).toBe(false)
    expect(defaults).not.toHaveProperty('meshId')
  })
  it('rejects missing booleans rather than silently enabling rearm', () => {
    const fixture = accessFixture()
    delete (fixture.slot.policy as Record<string, unknown>).rearm_enabled
    expect(() => decodeAccessMeshSlot(fixture)).toThrow(ConsoleContractError)
  })
})

describe('slot API transport', () => {
  it('posts the selected logical agent to managed status with the current token', async () => {
    const fetcher = vi.fn(async () => response(managedFixture()))
    const result = await new ConsoleClient('https://test.example', () => 'test-token', fetcher).managedSlotStatus('la_test')
    expect(result.logicalAgentId).toBe('la_test')
    expect(fetcher).toHaveBeenCalledWith(new URL('https://test.example/actions/persistent/managed/status'), expect.objectContaining({ method: 'POST', body: JSON.stringify({ logical_agent_id: 'la_test' }) }))
  })
  it('uses durable Access Mesh idempotency key unchanged and never auto-retries a network failure', async () => {
    const fetcher = vi.fn(async () => { throw new TypeError('offline') })
    const client = new ConsoleClient('https://test.example', () => 'test-token', fetcher)
    await expect(client.accessMeshMutation({ action: 'rotate', slot_id: 'as_test', expected_revision: 3, idempotency_key: 'stable-test-key' })).rejects.toMatchObject({ code: 'network_error', status: 0 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher).toHaveBeenCalledWith(new URL('https://test.example/actions/access/mutate'), expect.objectContaining({ body: JSON.stringify({ action: 'rotate', slot_id: 'as_test', expected_revision: 3, idempotency_key: 'stable-test-key' }) }))
  })
  it('handles old-server 404 as unsupported evidence, not an empty list', async () => {
    const client = new ConsoleClient('https://test.example', () => 'test-token', async () => response({ detail: 'Not Found' }, 404))
    await expect(client.accessMeshAllSlots()).rejects.toBeInstanceOf(ConsoleHttpError)
  })
  it('reads structured denial codes without exposing the server message', async () => {
    const client = new ConsoleClient('https://test.example', () => 'test-token', async () => response({ error: { code: 'insufficient_scope', message: 'private detail' } }, 403))
    await expect(client.accessMeshDefaults()).rejects.toMatchObject({ status: 403, code: 'insufficient_scope' })
  })
  it('collects opaque-cursor pages and fails on duplicate records rather than returning a partial list', async () => {
    const slot = accessFixture().slot
    const fetcher = vi.fn().mockResolvedValueOnce(response({ ok: true, slots: [slot], next_cursor: 'opaque+token' })).mockResolvedValueOnce(response({ ok: true, slots: [{ ...slot, slot_id: 'as_other' }], next_cursor: null }))
    const client = new ConsoleClient('https://test.example', () => 'test-token', fetcher)
    expect((await client.accessMeshAllSlots()).map((item) => item.slotId)).toEqual(['as_test', 'as_other'])
    expect(String(fetcher.mock.calls[1][0])).toContain('cursor=opaque%2Btoken')
    const repeated = new ConsoleClient('https://test.example', () => 'test-token', async () => response({ ok: true, slots: [slot], next_cursor: 'same' }))
    await expect(repeated.accessMeshAllSlots()).rejects.toThrow(ConsoleContractError)
  })
})
