import { beforeEach, expect, test } from 'vitest'
import { beginSlotOperation, clearSlotOperation, loadSlotOperation, SLOT_OPERATION_PREFIX, type PendingSlotOperation } from './operationJournal'

beforeEach(() => localStorage.clear())
const pending: PendingSlotOperation = { backend: 'access-mesh', request: { action: 'create', mode: 'persistent', idempotency_key: 'operation-one' }, startedAt: '2026-10-12T10:00:00Z', phase: 'pending' }

test('an uncertain native operation survives reopening and keeps the exact idempotency key', () => {
  beginSlotOperation('server-a', pending)
  expect(loadSlotOperation('server-a')).toEqual(pending)
  expect(() => beginSlotOperation('server-a', { ...pending, request: { action: 'create', mode: 'persistent', idempotency_key: 'operation-two' } })).toThrow('slot_operation_unresolved')
  expect(loadSlotOperation('server-b')).toBeNull()
  clearSlotOperation('server-a')
  expect(loadSlotOperation('server-a')).toBeNull()
})

test('responses and access codes are rejected rather than written into the operation journal', () => {
  const unsafe = { ...pending, request: { ...pending.request, access_code: '0042' } } as unknown as PendingSlotOperation
  expect(() => beginSlotOperation('server-a', unsafe)).toThrow('invalid_slot_operation')
  expect(localStorage.length).toBe(0)
  beginSlotOperation('server-a', pending)
  expect(localStorage.getItem(SLOT_OPERATION_PREFIX + 'server-a')).not.toContain('access_code')
})

test('invalid stored data blocks new work instead of silently losing uncertain operations', () => {
  localStorage.setItem(SLOT_OPERATION_PREFIX + 'server-a', '{broken')
  expect(() => loadSlotOperation('server-a')).toThrow('operation_journal_unavailable')
  expect(() => beginSlotOperation('server-a', pending)).toThrow('operation_journal_unavailable')
})

test('legacy markers contain only action and observed metadata, never a replayable unsafe mutation', () => {
  beginSlotOperation('server-a', { backend: 'persistent', action: 'rotate', logicalAgentId: 'la_a', observedGeneration: 2, startedAt: pending.startedAt, phase: 'pending' })
  expect(loadSlotOperation('server-a')).toEqual({ backend: 'persistent', action: 'rotate', logicalAgentId: 'la_a', observedGeneration: 2, startedAt: pending.startedAt, phase: 'pending' })
})


test('inheritance reset persists only the real native request and excludes copied policy or secret receipts', () => {
  const scope = 'isolated-native-reset'
  beginSlotOperation(scope, { backend: 'access-mesh', request: { action: 'policy', slot_id: 'as_isolated', expected_revision: 4, expected_parent_revision: 2, inherit_policy: true, idempotency_key: 'reset-isolated-4' }, startedAt: '2026-10-12T05:00:00Z', phase: 'pending' })
  const restored = loadSlotOperation(scope)
  expect(restored).toMatchObject({ request: { action: 'policy', inherit_policy: true, expected_revision: 4 } })
  expect(restored).not.toHaveProperty('request.policy')
  clearSlotOperation(scope)
})
