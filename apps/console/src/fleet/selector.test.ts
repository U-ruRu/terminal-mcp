import { expect, test } from 'vitest'
import {
  FleetIngressSelector,
  scoreFleetIngress,
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

test('one transient active failure marks suspect and never switches voluntarily', () => {
  const selector = new FleetIngressSelector({ probationSuccesses: 2, hysteresis: 5, cooldownMs: 0 })
  const boot = selector.observeProbe(probe('a'), 0)!
  selector.commitHandover(boot, 0)
  selector.observeProbe(probe('b', 'great'), 1)
  selector.transientFailure('a', 2)
  expect(selector.getState()).toBe('Suspect')
  expect(selector.getActiveCandidateId()).toBe('a')
})

test('better candidate needs probation and hysteresis before handover', () => {
  const selector = new FleetIngressSelector({ probationSuccesses: 2, hysteresis: 5, cooldownMs: 0 })
  const boot = selector.observeProbe(probe('a', 'bad'), 0)!
  selector.commitHandover(boot, 0)
  expect(selector.observeProbe(probe('b', 'great'), 1)).toBeNull()
  const decision = selector.observeProbe(probe('b', 'great'), 2)
  expect(decision).toMatchObject({
    fromCandidateId: 'a',
    toCandidateId: 'b',
    requiresSnapshot: false,
    resumeAfterProjectionSeq: 10,
    reason: 'better_candidate',
  })
})

test('hard loss only hands over to an authenticated compatible successful candidate', () => {
  const selector = new FleetIngressSelector({ cooldownMs: 0 })
  const boot = selector.observeProbe(probe('a'), 0)!
  selector.commitHandover(boot, 0)
  selector.hardFailure('a', 1)
  expect(selector.getState()).toBe('DegradedNoAlternative')
  expect(
    selector.observeProbe(
      { ...probe('b', 'great'), authenticated: false },
      2,
    ),
  ).toBeNull()
  const decision = selector.observeProbe(probe('b', 'great'), 3)
  expect(decision?.reason).toBe('hard_failover')
  expect(decision?.toCandidateId).toBe('b')
})

test('epoch mismatch mandates snapshot while same epoch resumes durable cursor', () => {
  const selector = new FleetIngressSelector({ probationSuccesses: 1, hysteresis: 0, cooldownMs: 0 })
  const boot = selector.observeProbe(probe('a', 'bad', 3, 22), 0)!
  selector.commitHandover(boot, 0)
  const same = selector.observeProbe(probe('b', 'great', 3, 30), 1)!
  expect(same.requiresSnapshot).toBe(false)
  expect(same.resumeAfterProjectionSeq).toBe(22)

  selector.commitHandover(same, 1)
  const different = selector.observeProbe(probe('c', 'great', 4, 2), 2)!
  expect(different.requiresSnapshot).toBe(true)
  expect(different.resumeAfterProjectionSeq).toBe(0)
})

test('network change decays confidence but does not force a switch', () => {
  const selector = new FleetIngressSelector({ cooldownMs: 0 })
  const boot = selector.observeProbe(probe('a'), 0)!
  selector.commitHandover(boot, 0)
  const scoreBefore = selector.qualitiesSnapshot()[0].score
  selector.networkChanged()
  expect(selector.getNetworkEpoch()).toBe(1)
  expect(selector.getActiveCandidateId()).toBe('a')
  expect(selector.getState()).toBe('Stable')
  expect(selector.qualitiesSnapshot()[0].score).toBeLessThan(scoreBefore)
})

test('background freezes quality tournament and cooldown prevents reconnect flapping', () => {
  const selector = new FleetIngressSelector({
    probationSuccesses: 1,
    hysteresis: 0,
    cooldownMs: 100,
  })
  const boot = selector.observeProbe(probe('a', 'bad'), 0)!
  selector.commitHandover(boot, 0)
  selector.setBackground(true)
  expect(selector.observeProbe(probe('b', 'great'), 200)).toBeNull()
  selector.setBackground(false)
  expect(selector.observeProbe(probe('b', 'great'), 50)).toBeNull()
  expect(selector.observeProbe(probe('b', 'great'), 101)?.toCandidateId).toBe('b')
})

test('unauthenticated or incompatible candidates are never score-eligible', () => {
  expect(scoreFleetIngress({ ...probe('a'), authenticated: false })).toBe(Number.NEGATIVE_INFINITY)
  expect(scoreFleetIngress({ ...probe('a'), compatible: false })).toBe(Number.NEGATIVE_INFINITY)
})
