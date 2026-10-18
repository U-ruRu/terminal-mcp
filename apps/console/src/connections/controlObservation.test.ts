import { expect,test } from 'vitest'
import { ConsoleContractError } from '../api/contracts'
import { ConsoleOperationError } from '../api/slotContracts'
import { coalesceControlReads } from '../fleet/controlRead'
import { meshControl } from '../slots/testFixtures'
import { ControlReadLifetime,mergeControlObservation,observeControl } from './controlObservation'

test.each([
  { failure: new ConsoleContractError('$.control.policy.rearm_enabled', 'expected boolean'), code: 'control_contract_invalid', path: '$.control.policy.rearm_enabled' },
  { failure: new ConsoleOperationError('mesh_transition_pending'), code: 'mesh_transition_pending', path: undefined },
])('control observation identifies $code without exposing exception text', async ({ failure, code, path }) => {
  const observed = await observeControl(async () => { throw failure })

  expect(observed).toMatchObject({ freshness: 'unknown', error: code, contractPath: path })
  expect(observed.error).not.toContain('expected boolean')
})

test('failed control observation preserves the timestamp of cached authority evidence', () => {
  const prior = { control: meshControl(), freshness: 'fresh' as const, observedAt: 123 }
  const failure = { freshness: 'unknown' as const, error: 'request_timeout', status: 0, elapsedMs: 20_000 }
  const merged = mergeControlObservation(prior, failure)
  expect(merged).toMatchObject({ control: prior.control, freshness: 'stale', observedAt: 123, error: 'request_timeout' })
})
test('concurrent control consumers share one in-flight read', async () => {
  let calls = 0
  const load = coalesceControlReads(async () => { calls++; return meshControl() })
  await Promise.all([load('alpha'), load('alpha')]); expect(calls).toBe(1)
})

test('starting a newer control read invalidates the pending read', () => {
  const lifetime = new ControlReadLifetime()
  lifetime.activate()
  const first = lifetime.begin()

  const second = lifetime.begin()

  expect(lifetime.isCurrent(first)).toBe(false)
  expect(lifetime.isCurrent(second)).toBe(true)
})
