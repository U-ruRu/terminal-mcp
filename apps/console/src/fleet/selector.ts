export type FleetIngressState =
  | 'Dormant'
  | 'RestoringPreferred'
  | 'Stable'
  | 'Suspect'
  | 'EvaluatingAlternative'
  | 'Handover'
  | 'DegradedNoAlternative'
  | 'NoGateway'

export type FleetIngressProbe = {
  candidateId: string
  authenticated: boolean
  compatible: boolean
  fleetId: string
  projectionEpoch: number
  projectionSeq: number
  rttMs: number
  successRate: number
  reconnectRate: number
  completeness: number
  freshness: number
  softLoad?: number
}

export type FleetIngressQuality = FleetIngressProbe & {
  score: number
  successes: number
  failures: number
  lastSuccessAt: number
  lastFailureAt?: number
}

export type FleetHandoverDecision = {
  fromCandidateId?: string
  toCandidateId: string
  projectionEpoch: number
  resumeAfterProjectionSeq: number
  requiresSnapshot: boolean
  reason: 'bootstrap' | 'better_candidate' | 'hard_failover'
}

export type FleetIngressSelectorOptions = {
  probationSuccesses?: number
  hysteresis?: number
  cooldownMs?: number
}

function bounded(value: number): number {
  return Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
}

export function scoreFleetIngress(probe: FleetIngressProbe): number {
  if (!probe.authenticated || !probe.compatible) return Number.NEGATIVE_INFINITY
  const rttPenalty = Math.min(18, Math.max(0, probe.rttMs) / 100)
  const loadPenalty = bounded(probe.softLoad ?? 0) * 5
  return (
    bounded(probe.successRate) * 35 +
    bounded(probe.completeness) * 25 +
    bounded(probe.freshness) * 25 -
    bounded(probe.reconnectRate) * 12 -
    rttPenalty -
    loadPenalty
  )
}

export class FleetIngressSelector {
  private readonly qualities = new Map<string, FleetIngressQuality>()
  private readonly probation = new Map<string, number>()
  private readonly probationSuccesses: number
  private readonly hysteresis: number
  private readonly cooldownMs: number
  private activeCandidateId?: string
  private preferredCandidateId?: string
  private lastSwitchAt = Number.NEGATIVE_INFINITY
  private background = false
  private hardLoss = false
  private networkEpoch = 0
  private state: FleetIngressState = 'Dormant'

  constructor(options: FleetIngressSelectorOptions = {}) {
    this.probationSuccesses = Math.max(1, options.probationSuccesses ?? 2)
    this.hysteresis = Math.max(0, options.hysteresis ?? 8)
    this.cooldownMs = Math.max(0, options.cooldownMs ?? 30_000)
  }

  restorePreferred(candidateId?: string): void {
    this.preferredCandidateId = candidateId
    this.state = candidateId ? 'RestoringPreferred' : 'Dormant'
  }

  getState(): FleetIngressState {
    return this.state
  }

  getActiveCandidateId(): string | undefined {
    return this.activeCandidateId
  }

  getPreferredCandidateId(): string | undefined {
    return this.preferredCandidateId
  }

  getNetworkEpoch(): number {
    return this.networkEpoch
  }

  qualitiesSnapshot(): FleetIngressQuality[] {
    return [...this.qualities.values()].map((item) => ({ ...item }))
  }

  setBackground(value: boolean): void {
    this.background = value
  }

  networkChanged(): void {
    this.networkEpoch += 1
    for (const [candidateId, quality] of this.qualities) {
      this.qualities.set(candidateId, {
        ...quality,
        successRate: quality.successRate * 0.9,
        freshness: quality.freshness * 0.9,
        score: quality.score * 0.9,
      })
    }
  }

  transientFailure(candidateId: string, now: number): void {
    const current = this.qualities.get(candidateId)
    if (current) {
      this.qualities.set(candidateId, {
        ...current,
        failures: current.failures + 1,
        lastFailureAt: now,
      })
    }
    this.probation.set(candidateId, 0)
    if (candidateId === this.activeCandidateId) this.state = 'Suspect'
  }

  hardFailure(candidateId: string, now: number): void {
    this.transientFailure(candidateId, now)
    if (candidateId !== this.activeCandidateId) return
    this.hardLoss = true
    this.state = this.bestEligibleAlternative(candidateId)
      ? 'EvaluatingAlternative'
      : 'DegradedNoAlternative'
  }

  observeProbe(probe: FleetIngressProbe, now: number): FleetHandoverDecision | null {
    const score = scoreFleetIngress(probe)
    const prior = this.qualities.get(probe.candidateId)
    const success = Number.isFinite(score)
    const quality: FleetIngressQuality = {
      ...probe,
      score,
      successes: (prior?.successes ?? 0) + (success ? 1 : 0),
      failures: prior?.failures ?? 0,
      lastSuccessAt: success ? now : (prior?.lastSuccessAt ?? 0),
      lastFailureAt: prior?.lastFailureAt,
    }
    this.qualities.set(probe.candidateId, quality)
    if (!success) {
      this.probation.set(probe.candidateId, 0)
      if (!this.activeCandidateId) this.state = 'NoGateway'
      return null
    }

    if (!this.activeCandidateId) {
      if (
        this.preferredCandidateId &&
        probe.candidateId !== this.preferredCandidateId &&
        this.qualities.get(this.preferredCandidateId)?.score !== undefined
      ) {
        return null
      }
      this.state = 'Handover'
      return {
        toCandidateId: probe.candidateId,
        projectionEpoch: probe.projectionEpoch,
        resumeAfterProjectionSeq: 0,
        requiresSnapshot: true,
        reason: 'bootstrap',
      }
    }

    if (probe.candidateId === this.activeCandidateId) {
      this.probation.set(probe.candidateId, 0)
      if (!this.hardLoss) this.state = 'Stable'
      return null
    }

    if (this.background) return null
    const active = this.qualities.get(this.activeCandidateId)
    if (this.hardLoss) {
      this.state = 'Handover'
      return this.handoverDecision(probe, 'hard_failover')
    }
    if (!active) return null
    if (now - this.lastSwitchAt < this.cooldownMs) return null
    if (score < active.score + this.hysteresis) {
      this.probation.set(probe.candidateId, 0)
      if (this.state !== 'Suspect') this.state = 'Stable'
      return null
    }
    this.state = 'EvaluatingAlternative'
    const count = (this.probation.get(probe.candidateId) ?? 0) + 1
    this.probation.set(probe.candidateId, count)
    if (count < this.probationSuccesses) return null
    this.state = 'Handover'
    return this.handoverDecision(probe, 'better_candidate')
  }

  commitHandover(decision: FleetHandoverDecision, now: number): void {
    this.activeCandidateId = decision.toCandidateId
    this.preferredCandidateId = decision.toCandidateId
    this.lastSwitchAt = now
    this.hardLoss = false
    this.probation.clear()
    this.state = 'Stable'
  }

  noGateway(): void {
    this.state = this.activeCandidateId ? 'DegradedNoAlternative' : 'NoGateway'
  }

  private handoverDecision(
    probe: FleetIngressProbe,
    reason: FleetHandoverDecision['reason'],
  ): FleetHandoverDecision {
    const active = this.activeCandidateId ? this.qualities.get(this.activeCandidateId) : undefined
    return {
      fromCandidateId: this.activeCandidateId,
      toCandidateId: probe.candidateId,
      projectionEpoch: probe.projectionEpoch,
      resumeAfterProjectionSeq:
        active && active.projectionEpoch === probe.projectionEpoch ? active.projectionSeq : 0,
      requiresSnapshot: !active || active.projectionEpoch !== probe.projectionEpoch,
      reason,
    }
  }

  private bestEligibleAlternative(excluded: string): FleetIngressQuality | undefined {
    return [...this.qualities.values()]
      .filter((item) => item.candidateId !== excluded && Number.isFinite(item.score))
      .sort((a, b) => b.score - a.score)[0]
  }
}
