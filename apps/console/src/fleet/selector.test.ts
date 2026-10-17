import { expect,test } from 'vitest'
import {
FleetIngressSelector,
type FleetIngressProbe,
} from './selector'

function probe(
  candidateId: string,
  score: 'good' | 'great' | 'bad' = 'good',
  epoch = 3,
  seq = 10,
): FleetIngressProbe {
  const quality =
    score === 'great'
      ? { rttMs: 20, successRate: 1, reconnectRate: 0, completeness: 1, freshness: 1 }
      : score === 'bad'
        ? { rttMs: 900, successRate: 0.5, reconnectRate: 0.4, completeness: 0.6, freshness: 0.5 }
        : { rttMs: 100, successRate: 0.95, reconnectRate: 0.05, completeness: 0.95, freshness: 0.95 }
  return {
    candidateId,
    authenticated: true,
    compatible: true,
    fleetId: 'fleet-a',
    projectionEpoch: epoch,
    projectionSeq: seq,
    ...quality,
  }
}

function selectorWithActiveIngress(): FleetIngressSelector {
  const selector = new FleetIngressSelector({ probationSuccesses: 1, hysteresis: 0, cooldownMs: 0 })
  const boot = selector.observeProbe(probe('a', 'bad', 3, 22), 0)!
  selector.commitHandover(boot, 0)
  return selector
}

test('verified candidate replaces a lost active ingress', () => {
  const selector = selectorWithActiveIngress()
  selector.hardFailure('a', 1)

  const decision = selector.observeProbe(probe('b', 'great'), 2)

  expect(decision).toMatchObject({ reason: 'hard_failover', toCandidateId: 'b' })
})

test('unauthenticated candidate cannot replace a lost active ingress', () => {
  const selector = selectorWithActiveIngress()
  selector.hardFailure('a', 1)

  const decision = selector.observeProbe({ ...probe('b', 'great'), authenticated: false }, 2)

  expect(decision).toBeNull()
  expect(selector.getActiveCandidateId()).toBe('a')
})

test('same epoch handover resumes the active ingress durable cursor', () => {
  const selector = selectorWithActiveIngress()

  const decision = selector.observeProbe(probe('b', 'great', 3, 30), 1)

  expect(decision).toMatchObject({ requiresSnapshot: false, resumeAfterProjectionSeq: 22 })
})
