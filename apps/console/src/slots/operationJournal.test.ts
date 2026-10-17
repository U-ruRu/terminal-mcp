import { beforeEach,expect,test } from 'vitest'
import { beginSlotOperation,loadSlotOperation,SLOT_OPERATION_PREFIX,type PendingSlotOperation } from './operationJournal'

beforeEach(() => localStorage.clear())
const pending: PendingSlotOperation = { backend: 'access-mesh', request: { action: 'create', mode: 'persistent', idempotency_key: 'operation-one' }, startedAt: '2026-10-12T10:00:00Z', phase: 'pending' }

test('an unresolved native request refuses replacement and retains its idempotency key', () => {
  beginSlotOperation('server-a', pending)

  expect(() => beginSlotOperation('server-a', { ...pending, request: { action: 'create', mode: 'persistent', idempotency_key: 'operation-two' } })).toThrow('slot_operation_unresolved')

  expect(loadSlotOperation('server-a')).toEqual(pending)
})
test('invalid stored data blocks new work instead of silently losing uncertain operations', () => {
  localStorage.setItem(SLOT_OPERATION_PREFIX + 'server-a', '{broken')
  expect(() => beginSlotOperation('server-a', pending)).toThrow('operation_journal_unavailable')
})

test('inheritance reset persists only the real native request and excludes copied policy or secret receipts', () => {
  const scope = 'isolated-native-reset'
  beginSlotOperation(scope, { backend: 'access-mesh', request: { action: 'policy', slot_id: 'as_isolated', expected_revision: 4, expected_parent_revision: 2, inherit_policy: true, idempotency_key: 'reset-isolated-4' }, startedAt: '2026-10-12T05:00:00Z', phase: 'pending' })
  const restored = loadSlotOperation(scope)
  expect(restored).toMatchObject({ request: { action: 'policy', inherit_policy: true, expected_revision: 4 } })
})
