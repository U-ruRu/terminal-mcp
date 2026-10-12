import { expect, test } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import { meshControl } from '../slots/testFixtures'
import { ControlReadLifetime, currentControlObservation, mergeControlObservation, observeControl } from './controlObservation'
import { coalesceControlReads } from '../fleet/controlRead'

test('failed Mesh request exposes only stable code/timing, retaining the last known revision without refreshing its timestamp', async () => {
  const read = await observeControl(async () => { throw new ConsoleHttpError(0, 'request_timeout', 'sensitive detail must not appear', '/actions/fleet/control', 20_000) })
  expect(read).toEqual({ freshness: 'unknown', error: 'request_timeout', status: 0, elapsedMs: 20_000 })
  const prior = { control: meshControl(), freshness: 'fresh' as const, observedAt: 123 }
  const merged = mergeControlObservation(prior, read)
  expect(merged).toMatchObject({ control: prior.control, freshness: 'stale', observedAt: 123, error: 'request_timeout' })
  expect(JSON.stringify(merged)).not.toContain('sensitive')
})
test('control observations age out independently from saved authentication', () => {
  const value = { control: meshControl(), freshness: 'fresh' as const, observedAt: 1_000 }
  expect(currentControlObservation(value, 30_000)).toBe(value)
  expect(currentControlObservation(value, 31_001)).toMatchObject({ freshness: 'stale', observedAt: 1_000, error: 'control_observation_expired' })
})
test('concurrent Mesh index and native slot grouping share one read but never reuse a previous successful response as fresh', async () => {
  let calls = 0
  const load = coalesceControlReads(async () => { calls++; return meshControl() })
  await Promise.all([load('alpha'), load('alpha')]); expect(calls).toBe(1)
  await load('alpha'); expect(calls).toBe(2)
})


test('a superseded or unmounted control read cannot publish and a pending batch prevents overlapping polling', () => {
  const lifetime = new ControlReadLifetime()
  lifetime.activate(); const first = lifetime.begin()
  expect(lifetime.idle()).toBe(false)
  const second = lifetime.begin()
  expect(lifetime.isCurrent(first)).toBe(false)
  expect(lifetime.isCurrent(second)).toBe(true)
  lifetime.finish(); expect(lifetime.idle()).toBe(false)
  lifetime.deactivate(); lifetime.activate()
  expect(lifetime.isCurrent(second)).toBe(false)
  lifetime.finish(); expect(lifetime.idle()).toBe(true)
})
