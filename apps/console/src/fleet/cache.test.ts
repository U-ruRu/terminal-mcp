import { expect, test } from 'vitest'
import { FleetCacheError, MemoryFleetProjectionCache } from './cache'
import type { FleetProjectionEventPage, FleetProjectionSnapshot } from './v1Types'

function snapshot(epoch = 4, seq = 10): FleetProjectionSnapshot {
  return {
    fleetId: 'fleet-a',
    nodeId: 'ingress-a',
    ownerNodeId: 'ingress-a',
    role: 'owner',
    projectionEpoch: epoch,
    projectionSeq: seq,
    updatedAt: '2026-09-30T18:00:00Z',
    sources: [{
      sourceNodeId: 'node-a',
      sourceStreamGeneration: 'gen-a',
      sourceSeq: 7,
      freshness: 'fresh',
      updatedAt: '2026-09-30T18:00:00Z',
    }],
    entities: [{
      sourceNodeId: 'node-a',
      entityType: 'command',
      entityId: 'cmd-1',
      entityRevision: 2,
      payloadVersion: 1,
      payload: { status: 'running' },
      sourceStreamGeneration: 'gen-a',
      sourceSeq: 7,
      projectionSeq: seq,
      updatedAt: '2026-09-30T18:00:00Z',
    }],
    runtimeOverlays: [{
      sourceNodeId: 'node-a',
      payload: { stale_running_commands: ['cmd-1'] },
      observedAt: '2026-09-30T18:00:01Z',
      freshness: 'fresh',
    }],
  }
}

function page(seq: number, status: string): FleetProjectionEventPage {
  return {
    projectionEpoch: 4,
    projectionSeq: seq,
    resetRequired: false,
    events: [{
      projectionEpoch: 4,
      projectionSeq: seq,
      eventId: 'event-' + seq,
      sourceNodeId: 'node-a',
      sourceStreamGeneration: 'gen-a',
      sourceSeq: seq,
      eventType: 'command.changed',
      entityType: 'command',
      entityId: 'cmd-1',
      entityRevision: seq,
      payloadVersion: 1,
      payload: { status },
      createdAt: '2026-09-30T18:00:' + seq + 'Z',
    }],
  }
}

test('overlay loss never changes durable running state or cursor', async () => {
  const cache = new MemoryFleetProjectionCache()
  const initial = await cache.applySnapshot(snapshot())
  expect(initial.appliedProjectionSeq).toBe(10)
  expect(initial.entities[0].payload.status).toBe('running')
  await cache.replaceRuntimeOverlays([])
  const after = await cache.restore()
  expect(after.appliedProjectionSeq).toBe(10)
  expect(after.entities[0].payload.status).toBe('running')
  expect(after.runtimeOverlays).toEqual([])
})

test('durable terminal event advances entity and cursor together', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot())
  const completed = await cache.applyEvents(page(11, 'completed'))
  expect(completed.appliedProjectionSeq).toBe(11)
  expect(completed.entities[0].payload.status).toBe('completed')
})

test('gap and epoch mismatch fail closed without cursor advancement', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot())
  await expect(cache.applyEvents(page(12, 'completed'))).rejects.toMatchObject({
    code: 'projection_cursor_gap',
  })
  expect((await cache.restore()).appliedProjectionSeq).toBe(10)
  await expect(
    cache.applyEvents({ ...page(11, 'completed'), projectionEpoch: 5 }),
  ).rejects.toBeInstanceOf(FleetCacheError)
  expect((await cache.restore()).appliedProjectionSeq).toBe(10)
})

test('new epoch replaces entities and keeps preferred ingress', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot())
  await cache.setPreferredIngress('ingress-b')
  const nextSnapshot = snapshot(5, 1)
  nextSnapshot.entities = [{
    ...nextSnapshot.entities[0],
    entityId: 'cmd-new',
    projectionSeq: 1,
    payload: { status: 'queued' },
  }]
  const next = await cache.applySnapshot(nextSnapshot)
  expect(next.projectionEpoch).toBe(5)
  expect(next.appliedProjectionSeq).toBe(1)
  expect(next.preferredIngressId).toBe('ingress-b')
  expect(next.entities.map((item) => item.entityId)).toEqual(['cmd-new'])
  expect(next.activity).toEqual([])
})

test('bounded activity evicts old events without losing entity state', async () => {
  const cache = new MemoryFleetProjectionCache(2)
  await cache.applySnapshot(snapshot())
  await cache.applyEvents(page(11, 'running'))
  await cache.applyEvents(page(12, 'running'))
  const final = await cache.applyEvents(page(13, 'completed'))
  expect(final.activity.map((item) => item.projectionSeq)).toEqual([12, 13])
  expect(final.entities[0].payload.status).toBe('completed')
})

test('materialized projection.remove deletes entity instead of caching empty payload', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot())
  const removed: FleetProjectionEventPage = {
    projectionEpoch: 4,
    projectionSeq: 11,
    resetRequired: false,
    events: [{
      projectionEpoch: 4,
      projectionSeq: 11,
      eventId: 'event-remove-11',
      sourceNodeId: 'node-a',
      sourceStreamGeneration: 'gen-a',
      sourceSeq: 11,
      eventType: 'projection.remove',
      entityType: 'command',
      entityId: 'cmd-1',
      entityRevision: 11,
      payloadVersion: 2,
      payload: {},
      createdAt: '2026-09-30T18:00:11Z',
    }],
  }
  const next = await cache.applyEvents(removed)
  expect(next.appliedProjectionSeq).toBe(11)
  expect(next.entities).toEqual([])
})
