import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import type { FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import { Icon } from './Icon'
import { ResourceMetric } from './ResourceMetric'
import { StatusBadge } from './StatusBadge'
import { resourceVisualState, serverVisualState, type ServerVisualState } from './serverPresentation'

function statusLabel(state: ServerVisualState, t: ReturnType<typeof useI18n>['t']): string {
  if (state === 'healthy') return t('status.live')
  if (state === 'stale') return t('status.stale')
  if (state === 'offline') return t('status.offline')
  if (state === 'loading') return t('status.catchingUp')
  return t('fleet.needsAttention')
}

function connectionStateLabel(value: FleetServerReadModel['connectionState'], t: ReturnType<typeof useI18n>['t']): string {
  if (value === 'live') return t('status.live')
  if (value === 'offline') return t('status.offline')
  return t('status.catchingUp')
}

function freshnessLabel(value: FleetServerReadModel['freshness'], t: ReturnType<typeof useI18n>['t']): string {
  if (value === 'fresh') return t('status.fresh')
  if (value === 'stale') return t('status.stale')
  if (value === 'catching_up') return t('status.catchingUp')
  if (value === 'offline') return t('status.offline')
  return t('status.loading')
}

type ServerCardProps = {
  server: FleetServerReadModel
  variant?: 'compact' | 'large'
  interactive?: boolean
}

export function ServerCard({
  server,
  variant = 'compact',
  interactive = variant === 'compact',
}: ServerCardProps) {
  const { t, number } = useI18n()
  const state = serverVisualState(server)
  const resourceKinds = ['cpu', 'memory', 'filesystem'] as const
  const resourceStates = resourceKinds.map((kind) => resourceVisualState(server, kind))
  const resourceAttention = resourceStates.some((resourceState) => resourceState === 'attention')
  const resourceIssue = server.connectivity !== 'offline' && resourceStates.some((resourceState) => resourceState !== 'normal')
  const connectionProblem = server.connectivity === 'offline' || server.connectivity === 'connecting' || server.connectivity === 'reconnecting' || server.freshness === 'stale' || server.freshness === 'catching_up'
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!connectionProblem || !server.stateSince) return
    const handle = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(handle)
  }, [connectionProblem, server.stateSince])
  const problemStartedAt = server.stateSince ? Date.parse(server.stateSince) : Number.NaN
  const problemSeconds = connectionProblem && Number.isFinite(problemStartedAt) ? Math.max(0, Math.floor((now - problemStartedAt) / 1000)) : undefined

  const friendlyReason = (raw: string): string => {
    const normalized = raw.toLowerCase()
    if (normalized.includes('network_error') || normalized === 'lost') return t('fleet.problem.network')
    if (normalized.includes('cursor_gap')) return t('fleet.problem.cursorGap')
    if (normalized.includes('heartbeat_cursor_ahead')) return t('fleet.problem.heartbeatAhead')
    if (normalized.includes('snapshot_refresh_lag')) return t('fleet.problem.snapshotLag')
    if (normalized.includes('snapshot_refresh_failed')) return t('fleet.problem.snapshotFailed')
    if (normalized.includes('journal_gap')) return t('fleet.problem.journalGap')
    if (normalized.includes('invalid_cursor')) return t('fleet.problem.invalidCursor')
    if (normalized.includes('unauthorized')) return t('fleet.problem.unauthorized')
    if (normalized.includes('revoked')) return t('fleet.problem.revoked')
    if (normalized.includes('expired')) return t('fleet.problem.expired')
    const human = raw.replace(/[_:.-]+/g, ' ').replace(/\s+/g, ' ').trim()
    return human ? human.charAt(0).toUpperCase() + human.slice(1) : t('fleet.problem.unknown')
  }
  const reasons = new Set<string>()
  if (server.connectivity === 'offline') reasons.add(t('fleet.problem.noConnection'))
  if (server.connectivity === 'connecting' || server.connectivity === 'reconnecting') reasons.add(t('fleet.problem.synchronizing'))
  if (server.freshness === 'catching_up') reasons.add(t('fleet.problem.synchronizing'))
  if (server.freshness === 'stale') reasons.add(server.staleReason ? friendlyReason(server.staleReason) : t('fleet.problem.stale'))
  if (server.lastError) reasons.add(friendlyReason(server.lastError))
  if (server.healthy === false) reasons.add(t('fleet.problem.unhealthy'))
  if (resourceAttention) reasons.add(t('fleet.problem.resourceAttention'))
  const problemReasons = [...reasons]

  const content = (
    <>
      {variant === 'compact' ? (
        <div className="server-card-heading">
          <div className="server-card-identity">
            <span className="server-card-icon" aria-hidden="true"><Icon name="server" /></span>
            <div>
              <h3>{server.displayName}</h3>
              <p>{server.version ?? t('fleet.versionUnavailable')}</p>
            </div>
          </div>
          <StatusBadge state={state} label={statusLabel(state, t)} />
        </div>
      ) : (
        <div className="server-runtime-summary">
          <div className="server-state-strip" aria-label={server.displayName + ' runtime state'}>
            <span className={'status server-status status-' + server.connectionState}>{connectionStateLabel(server.connectionState, t)}</span>
            {freshnessLabel(server.freshness, t) !== connectionStateLabel(server.connectionState, t) ? (
              <span className={'status server-status fleet-status-' + server.freshness}>{freshnessLabel(server.freshness, t)}</span>
            ) : null}
            {state === 'attention' || state === 'critical' ? <StatusBadge state={state} label={statusLabel(state, t)} /> : null}
          </div>
          <p className="muted">{t('server.version')}: <strong>{server.version ?? t('fleet.versionUnavailable')}</strong></p>
        </div>
      )}

      <dl className={'server-card-metrics' + (resourceIssue ? ' server-card-metrics-attention' : '')} aria-label={t('server.hostResources')}>
        <ResourceMetric server={server} kind="cpu" label={t('common.cpu')} />
        <ResourceMetric server={server} kind="memory" label={t('common.ram')} />
        <ResourceMetric server={server} kind="filesystem" label={t('common.disk')} />
      </dl>

      {variant === 'compact' ? (
        <p className="server-card-problem">{problemReasons.length > 0 ? <>{problemReasons.join(' · ')}{problemSeconds !== undefined ? ` · ${number(problemSeconds)}s` : ''}</> : '\u00a0'}</p>
      ) : null}

      {variant === 'large' ? (
        <div className="server-card-large-summary">
          <div className="server-card-agent-count">
            <span>{t('server.agents')}</span>
            <strong>{number(server.activeAgentCount)}</strong>
          </div>
          <dl className="server-card-task-summary" aria-label={t('server.summary')}>
            <div><dt>{t('fleet.ready')}</dt><dd>{number(server.taskCounts.ready)}</dd></div>
            <div><dt>{t('fleet.blocked')}</dt><dd>{number(server.taskCounts.blocked)}</dd></div>
            <div><dt>{t('tasks.completed')}</dt><dd>{number(server.taskCounts.done)}</dd></div>
          </dl>
          <div className="server-card-communication">
            <span>{t('server.alerts')} <strong>{number(server.communication.alerts)}</strong></span>
            <span>{t('server.replies')} <strong>{number(server.communication.replyRequired)}</strong></span>
          </div>
        </div>
      ) : null}

      {variant === 'compact' ? null : state === 'stale' && server.lastSeenAt ? (
        <p className="server-card-age">{server.lastSeenAt.replace('T', ' ').replace('Z', ' UTC')}</p>
      ) : null}
      {interactive ? <span className="server-card-chevron" aria-hidden="true"><Icon name="chevron-right" /></span> : null}
    </>
  )

  return (
    <article
      className={'server-card server-card-' + variant + ' server-card-' + state}
      data-state={state}
      data-variant={variant}
      aria-label={server.displayName + ' ' + t('fleet.serverSuffix')}
    >
      {interactive ? (
        <Link
          className="server-card-link"
          to={'/servers/' + encodeURIComponent(server.instanceId)}
          aria-label={server.displayName + ' · ' + statusLabel(state, t)}
        >
          {content}
        </Link>
      ) : (
        <div className="server-card-link server-card-static">{content}</div>
      )}
    </article>
  )
}
