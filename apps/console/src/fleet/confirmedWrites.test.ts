import { expect, test } from 'vitest'
import { confirmedWritesFromPersistentMutation } from './confirmedWrites'

test('creates a durable overlay only from authoritative successful slot response', () => {
  const writes = confirmedWritesFromPersistentMutation(
    'node-a',
    '/actions/persistent/slots/play',
    { logical_agent_id: 'la-1', expected_revision: 7, idempotency_key: 'idem-12345678' },
    {
      ok: true,
      payload: {
        ok: true,
        slot: {
          logical_agent_id: 'la-1',
          state: 'armed',
          slot_revision: 8,
          authority_epoch: 3,
        },
      },
    },
    123,
  )
  expect(writes).toEqual([expect.objectContaining({
    requestId: 'idem-12345678',
    sourceNodeId: 'node-a',
    entityId: 'la-1',
    entityRevision: 8,
    authorityEpoch: 3,
    payloadPatch: expect.objectContaining({ state: 'armed' }),
  })])
})

test('delete confirmation becomes a local tombstone overlay', () => {
  const writes = confirmedWritesFromPersistentMutation(
    'node-a',
    '/actions/persistent/slots/delete',
    { logical_agent_id: 'la-1', idempotency_key: 'idem-delete-1' },
    {
      ok: true,
      payload: {
        ok: true,
        slot: { logical_agent_id: 'la-1', state: 'deleted', slot_revision: 9 },
      },
    },
  )
  expect(writes[0].remove).toBe(true)
})


test('Access-code mutation secrets never become durable confirmed-write overlays', () => {
  const writes = confirmedWritesFromPersistentMutation(
    'node-a',
    '/actions/persistent/slots/migrate-access',
    { logical_agent_id: 'la-1' },
    {
      ok: true,
      payload: {
        ok: true,
        access: {
          logical_agent_id: 'la-1',
          public_name: 'Alpha',
          access_generation: 1,
          access_code: 'ZQPH',
        },
      },
    },
  )
  expect(writes).toEqual([])
  expect(JSON.stringify(writes)).not.toContain('ZQPH')
})
