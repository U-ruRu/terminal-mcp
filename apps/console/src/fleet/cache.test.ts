import { expect,test } from 'vitest'
import { MemoryFleetProjectionCache,reduceEvents,reduceSnapshot } from './cache'
import type { FleetConfirmedWrite,FleetDurableCacheState,FleetProjectionEventPage,FleetProjectionSnapshot } from './v1Types'

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

test('durable terminal event advances entity and cursor together', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot())
  const completed = await cache.applyEvents(page(11, 'completed'))
  expect(completed.appliedProjectionSeq).toBe(11)
  expect(completed.entities[0].payload.status).toBe('completed')
})

test.each([
  { fault: 'cursor gap', seq: 12, epoch: 4, code: 'projection_cursor_gap' },
  { fault: 'changed epoch', seq: 11, epoch: 5, code: 'projection_epoch_changed' },
])('rejects $fault without advancing the durable cursor', async ({ seq, epoch, code }) => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot())
  const incoming = { ...page(seq, 'completed'), projectionEpoch: epoch }

  await expect(cache.applyEvents(incoming)).rejects.toMatchObject({ code })

  expect((await cache.restore()).appliedProjectionSeq).toBe(10)
})

test('new epoch snapshot replaces the cached generation and preserves ingress preference', () => {
  const prior = durableState()
  prior.preferredIngressId = 'ingress-b'
  const incoming = snapshot(5, 1)
  incoming.entities = [{
    ...incoming.entities[0],
    entityId: 'cmd-new',
    projectionSeq: 1,
    payload: { status: 'queued' },
  }]

  const next = reduceSnapshot(prior, incoming)

  expect(next).toMatchObject({
    projectionEpoch: 5,
    appliedProjectionSeq: 1,
    preferredIngressId: 'ingress-b',
  })
  expect(next.entities.map((item) => item.entityId)).toEqual(['cmd-new'])
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

function durableState(): FleetDurableCacheState {
  const initial = snapshot()
  return {
    version: 1,
    fleetId: initial.fleetId,
    projectionEpoch: initial.projectionEpoch,
    appliedProjectionSeq: initial.projectionSeq,
    entities: initial.entities,
    sources: initial.sources,
    activity: [],
    gatewayQuality: [],
    updatedAt: 1,
  }
}

test.each([
  { observedRevision: 7, retainedRequestIds: ['idem-1'] },
  { observedRevision: 8, retainedRequestIds: [] },
])('reconciles the pending write against projection revision $observedRevision', ({ observedRevision, retainedRequestIds }) => {
  const write: FleetConfirmedWrite = {
    requestId: 'idem-1',
    sourceNodeId: 'node-a',
    entityType: 'logical_agent',
    entityId: 'logical-1',
    authorityEpoch: 3,
    entityRevision: 8,
    payloadPatch: { state: 'armed', slot_revision: 8, authority_epoch: 3 },
    createdAt: 1,
    state: 'confirmed_pending_projection',
  }
  const prior = { ...durableState(), confirmedWrites: [write] }
  const incoming = page(11, 'ignored')
  incoming.events[0] = {
    ...incoming.events[0],
    entityType: 'logical_agent',
    entityId: 'logical-1',
    entityRevision: observedRevision,
    authorityEpoch: 3,
    payload: { state: 'armed', slot_revision: observedRevision, authority_epoch: 3 },
  }

  const next = reduceEvents(prior, incoming)

  expect(next.confirmedWrites?.map((item) => item.requestId)).toEqual(retainedRequestIds)
})
