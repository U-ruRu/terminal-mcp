import { expect,test,vi } from 'vitest'
import { FleetAdaptiveReadRuntime,type FleetIngressEndpoint } from './adaptiveRuntime'
import { MemoryFleetProjectionCache } from './cache'
import { FleetIngressSelector } from './selector'
import type { FleetProjectionEventPage,FleetProjectionSnapshot } from './v1Types'

function snapshot(candidateId: string, epoch = 3, seq = 10, hard = '2099-01-01T00:00:00Z'): FleetProjectionSnapshot {
  return {
    fleetId: 'fleet-a',
    nodeId: candidateId,
    ownerNodeId: 'a',
    role: candidateId === 'a' ? 'owner' : 'follower',
    projectionEpoch: epoch,
    projectionSeq: seq,
    updatedAt: 'now',
    sources: [],
    entities: [{
      sourceNodeId: 'home',
      entityType: 'work_session',
      entityId: 'ws-1',
      entityRevision: 1,
      payloadVersion: 1,
      payload: { hard_expires_at: hard, session_epoch: 7 },
      sourceStreamGeneration: 'g',
      sourceSeq: 1,
      projectionSeq: seq,
      updatedAt: 'now',
    }],
    runtimeOverlays: [],
  }
}

function endpoint(id: string, epoch = 3, seq = 10, score = 0.9): FleetIngressEndpoint {
  return {
    candidateId: id,
    probe: vi.fn(async () => ({
      candidateId: id,
      authenticated: true,
      compatible: true,
      fleetId: 'fleet-a',
      projectionEpoch: epoch,
      projectionSeq: seq,
      rttMs: score > 0.95 ? 10 : 100,
      successRate: score,
      reconnectRate: 0,
      completeness: score,
      freshness: score,
    })),
    snapshot: vi.fn(async () => snapshot(id, epoch, seq)),
    events: vi.fn(async (since): Promise<FleetProjectionEventPage> => ({
      projectionEpoch: epoch,
      projectionSeq: seq,
      resetRequired: false,
      events: [],
      oldestProjectionSeq: Math.min(since + 1, seq),
      newestProjectionSeq: seq,
    })),
  }
}

test('bootstrap uses one ingress and persists preference', async () => {
  const cache = new MemoryFleetProjectionCache()
  const runtime = new FleetAdaptiveReadRuntime(cache)
  const a = endpoint('a')
  const b = endpoint('b')
  expect(await runtime.start([a, b])).toBe(true)
  expect(runtime.getState().activeIngressId).toBe('a')
  expect(runtime.getState().preferredIngressId).toBe('a')
  expect(a.snapshot).toHaveBeenCalledTimes(1)
  expect(b.snapshot).not.toHaveBeenCalled()
})

test('epoch mismatch requires snapshot replacement', async () => {
  const cache = new MemoryFleetProjectionCache()
  const selector = new FleetIngressSelector({ probationSuccesses: 1, hysteresis: 0, cooldownMs: 0 })
  const runtime = new FleetAdaptiveReadRuntime(cache, selector)
  const a = endpoint('a', 3, 10, 0.5)
  const b = endpoint('b', 4, 2, 1)
  await runtime.start([a])
  await runtime.evaluate([b])
  expect(b.snapshot).toHaveBeenCalledTimes(1)
  expect(runtime.getState().projectionEpoch).toBe(4)
})

test('runtime rejects a successful probe from another fleet before handover', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot('a'))
  const runtime = new FleetAdaptiveReadRuntime(cache)
  const bad = endpoint('bad')
  bad.probe = vi.fn(async () => ({
    candidateId: 'bad', authenticated: true, compatible: true, fleetId: 'fleet-b',
    projectionEpoch: 3, projectionSeq: 10, rttMs: 1, successRate: 1,
    reconnectRate: 0, completeness: 1, freshness: 1,
  }))
  expect(await runtime.start([bad])).toBe(false)
  expect(bad.snapshot).not.toHaveBeenCalled()
})

test('steady catch-up drains multiple event pages without snapshot recovery', async () => {
  const cache = new MemoryFleetProjectionCache()
  const runtime = new FleetAdaptiveReadRuntime(cache)
  const a = endpoint('a', 3, 10)
  await runtime.start([a])
  a.snapshot = vi.fn(async () => snapshot('a', 3, 12))
  a.events = vi.fn(async (since): Promise<FleetProjectionEventPage> => {
    const seq = since + 1
    return {
      projectionEpoch: 3,
      projectionSeq: 12,
      resetRequired: false,
      events: seq <= 12 ? [{
        projectionEpoch: 3,
        projectionSeq: seq,
        eventId: 'event-' + seq,
        sourceNodeId: 'home',
        sourceStreamGeneration: 'g',
        sourceSeq: seq,
        eventType: 'command.changed',
        entityType: 'command',
        entityId: 'cmd-' + seq,
        entityRevision: seq,
        payloadVersion: 2,
        payload: { status: 'running' },
        createdAt: 'now',
      }] : [],
    }
  })
  await runtime.syncOnce()
  expect(a.events).toHaveBeenCalledTimes(2)
  expect(a.events).toHaveBeenNthCalledWith(1, 10)
  expect(a.events).toHaveBeenNthCalledWith(2, 11)
  expect(a.snapshot).not.toHaveBeenCalled()
  expect(runtime.getState().projectionSeq).toBe(12)
})

test('concurrent sync requests share one flight and coalesce to one trailing catch-up pass', async () => {
  const cache = new MemoryFleetProjectionCache()
  const runtime = new FleetAdaptiveReadRuntime(cache)
  const a = endpoint('a')
  await runtime.start([a])
  let release!: () => void
  let first = true
  const gate = new Promise<void>((resolve) => { release = resolve })
  a.events = vi.fn(async (since): Promise<FleetProjectionEventPage> => {
    if (first) {
      first = false
      await gate
    }
    return {
      projectionEpoch: 3,
      projectionSeq: since,
      resetRequired: false,
      events: [],
      oldestProjectionSeq: since,
      newestProjectionSeq: since,
    }
  })

  const one = runtime.syncOnce()
  const two = runtime.syncOnce()
  const three = runtime.syncOnce()
  await vi.waitFor(() => expect(a.events).toHaveBeenCalledTimes(1))
  release()
  await Promise.all([one, two, three])
  expect(a.events).toHaveBeenCalledTimes(2)
  expect(runtime.getState().status).toBe('live')
})
