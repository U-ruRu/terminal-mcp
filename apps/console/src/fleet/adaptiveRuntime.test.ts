import { expect, test, vi } from 'vitest'
import { FleetAdaptiveReadRuntime, type FleetIngressEndpoint } from './adaptiveRuntime'
import { MemoryFleetProjectionCache } from './cache'
import { FleetIngressSelector } from './selector'
import type { FleetProjectionEventPage, FleetProjectionSnapshot } from './v1Types'

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

test('same epoch handover resumes durable cursor without snapshot or extending hard D', async () => {
  const cache = new MemoryFleetProjectionCache()
  const selector = new FleetIngressSelector({ probationSuccesses: 1, hysteresis: 0, cooldownMs: 0 })
  const runtime = new FleetAdaptiveReadRuntime(cache, selector, (() => {
    let value = 0
    return () => ++value
  })())
  const a = endpoint('a', 3, 10, 0.5)
  const b = endpoint('b', 3, 10, 1)
  await runtime.start([a])
  const hardBefore = runtime.getState().cache.entities[0].payload.hard_expires_at
  await runtime.evaluate([b])
  expect(runtime.getState().activeIngressId).toBe('b')
  expect(b.snapshot).not.toHaveBeenCalled()
  expect(b.events).toHaveBeenCalledWith(10)
  expect(runtime.getState().cache.entities[0].payload.hard_expires_at).toBe(hardBefore)
  expect(runtime.getState().cache.entities[0].payload.session_epoch).toBe(7)
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

test('hard failover ignores failed alternative handshake and falls back if none succeeds', async () => {
  const cache = new MemoryFleetProjectionCache()
  const runtime = new FleetAdaptiveReadRuntime(cache)
  const a = endpoint('a')
  const b = endpoint('b')
  b.probe = vi.fn(async () => { throw new Error('network_error') })
  await runtime.start([a])
  expect(await runtime.hardFailActive()).toBe(false)
  expect(runtime.getState().activeIngressId).toBe('a')
  expect(runtime.getState().status).toBe('degraded')
})

test('cached fleet identity rejects a cross-fleet ingress snapshot', async () => {
  const cache = new MemoryFleetProjectionCache()
  await cache.applySnapshot(snapshot('a'))
  const runtime = new FleetAdaptiveReadRuntime(cache)
  const bad = endpoint('bad')
  bad.snapshot = vi.fn(async () => ({ ...snapshot('bad'), fleetId: 'fleet-b' }))
  expect(await runtime.start([bad])).toBe(false)
  expect(runtime.getState().status).toBe('fallback')
})

test('network changes never force handover and background freezes tournament', async () => {
  const cache = new MemoryFleetProjectionCache()
  const selector = new FleetIngressSelector({ probationSuccesses: 1, hysteresis: 0, cooldownMs: 0 })
  const runtime = new FleetAdaptiveReadRuntime(cache, selector)
  const a = endpoint('a', 3, 10, 0.5)
  const b = endpoint('b', 3, 10, 1)
  await runtime.start([a])
  runtime.networkChanged()
  expect(runtime.getState().activeIngressId).toBe('a')
  runtime.setBackground(true)
  await runtime.evaluate([b])
  expect(runtime.getState().activeIngressId).toBe('a')
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
