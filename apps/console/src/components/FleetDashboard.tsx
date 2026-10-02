import { useMemo, useRef, useState } from 'react'

import type { FleetReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import { ServerCard } from './ServerCard'
import { SegmentedControl } from './UiPrimitives'
import { needsAttention, serverAlphaSort, serverProblemSort, serverVisualState } from './serverPresentation'

type FleetFilter = 'all' | 'attention' | 'live'

export function FleetDashboard({ model }: { model: FleetReadModel }) {
  const { t, number } = useI18n()
  const [filter, setFilter] = useState<FleetFilter>('all')
  const serverListRef = useRef<HTMLDivElement>(null)
  const problemCount = model.servers.filter(needsAttention).length
  const fleetState = problemCount > 0
    ? 'attention'
    : model.servers.some((server) => serverVisualState(server) === 'loading')
      ? 'loading'
      : 'healthy'

  const focusServerList = (nextFilter: FleetFilter) => {
    setFilter(nextFilter)
    serverListRef.current?.scrollIntoView?.({ block: 'start' })
    serverListRef.current?.focus({ preventScroll: true })
  }

  const servers = useMemo(() => {
    const visible =
      filter === 'attention'
        ? model.servers.filter(needsAttention)
        : filter === 'live'
          ? model.servers.filter((server) => server.connectivity === 'live')
          : model.servers
    return [...visible].sort(filter === 'attention' ? serverProblemSort : serverAlphaSort)
  }, [filter, model.servers])

  return (
    <section className="stack" aria-labelledby="fleet-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t('fleet.eyebrow')}</p>
          <h2 id="fleet-title">{t('fleet.title')}</h2>
          <p className="muted">{t('fleet.description')}</p>
        </div>
        <span className="environment-badge">{number(model.summary.totalServers)} {t('fleet.servers')}</span>
      </div>

      <button
        type="button"
        className={'fleet-status-card fleet-status-card-' + fleetState}
        aria-label={
          fleetState === 'attention'
            ? t('fleet.needsAttention') + ': ' + number(problemCount) + ' ' + t('fleet.servers')
            : fleetState === 'loading'
              ? t('status.catchingUp')
              : t('fleet.live') + ': ' + number(model.summary.totalServers) + ' ' + t('fleet.servers')
        }
        onClick={() => focusServerList(fleetState === 'attention' ? 'attention' : 'all')}
      >
        <span className="fleet-status-icon" aria-hidden="true">{fleetState === 'healthy' ? '✓' : fleetState === 'attention' ? '!' : '…'}</span>
        <span>
          <strong>{fleetState === 'attention' ? t('fleet.needsAttention') : fleetState === 'loading' ? t('status.catchingUp') : t('fleet.live')}</strong>
          <small>
            {fleetState === 'attention'
              ? number(problemCount) + ' / ' + number(model.summary.totalServers) + ' ' + t('fleet.servers')
              : number(model.summary.totalServers) + ' ' + t('fleet.servers')}
          </small>
        </span>
        <span className="fleet-status-chevron" aria-hidden="true">›</span>
      </button>

      <div className="fleet-summary" aria-label={t('fleet.totals')}>
        <article className="card"><span>{t('fleet.live')}</span><strong>{number(model.summary.liveServers)}</strong></article>
        <article className="card"><span>{t('fleet.stale')}</span><strong>{number(model.summary.staleServers)}</strong></article>
        <article className="card"><span>{t('fleet.offline')}</span><strong>{number(model.summary.offlineServers)}</strong></article>
        <article className="card"><span>{t('fleet.activeAgents')}</span><strong>{number(model.summary.activeAgents)}</strong></article>
        <article className="card"><span>{t('fleet.blockedTasks')}</span><strong>{number(model.summary.blockedTasks)}</strong></article>
        <article className="card"><span>{t('fleet.alerts')}</span><strong>{number(model.summary.alerts)}</strong></article>
      </div>

      <SegmentedControl
        label={t('fleet.filterServers')}
        value={filter}
        onChange={(value) => setFilter(value as FleetFilter)}
        options={[
          { value: 'all', label: t('common.all') + ' (' + number(model.summary.totalServers) + ')' },
          { value: 'attention', label: t('fleet.needsAttention') },
          { value: 'live', label: t('fleet.live') },
        ]}
      />

      <div
        ref={serverListRef}
        className="fleet-grid fleet-grid-compact"
        tabIndex={-1}
        aria-label={t('nav.servers')}
      >
        {servers.map((server) => <ServerCard key={server.instanceId} server={server} />)}
      </div>

      <article className="panel">
        <div>
          <p className="eyebrow">{t('fleet.sharedSessions')}</p>
          <h3>{t('fleet.agentContinuity')}</h3>
        </div>
        {model.sessions.length === 0 ? (
          <p className="muted">{t('fleet.noActiveSharedSessions')}</p>
        ) : (
          <ul className="session-list">
            {model.sessions.map((session) => (
              <li key={session.sessionRef} aria-label={session.name + ' ' + t('fleet.globalSession')}>
                <strong>{session.name}</strong>
                <span>
                  {t('fleet.origin')} {session.originInstanceId ?? t('common.unavailable')} ·{' '}
                  {session.attachments.length} {t('fleet.attached')}
                </span>
                <small>
                  {t('fleet.age')} {session.sessionAgeSeconds ?? '—'}s · {t('fleet.remaining')}{' '}
                  {session.sessionRemainingSeconds ?? '—'}s
                </small>
                <small>
                  {(session.scopedIntents ?? []).length > 0
                    ? (session.scopedIntents ?? [])
                        .map(
                          (item) =>
                            item.displayName +
                            ': ' +
                            item.intent +
                            ' (' +
                            (item.status === 'fresh' ? t('fleet.live') : t('fleet.stale')) +
                            ', ' +
                            number(item.ageSeconds) +
                            's)',
                        )
                        .join(' · ')
                    : session.attachments
                        .map((item) => item.displayName + ': ' + item.intent)
                        .join(' · ')}
                </small>
              </li>
            ))}
          </ul>
        )}
      </article>

      <article className="panel">
        <div>
          <p className="eyebrow">{t('fleet.recentActivity')}</p>
          <h3>{t('fleet.acrossFleet')}</h3>
        </div>
        {model.recentActivity.length === 0 ? (
          <p className="muted">{t('fleet.noRecentActivity')}</p>
        ) : (
          <ol className="activity-list">
            {model.recentActivity.slice(0, 8).map((item, index) => (
              <li key={item.instanceId + '-' + (item.sequence ?? item.occurredAt) + '-' + index}>
                <strong>{item.displayName}</strong>
                <span>{item.label}</span>
                <small>{item.detail}</small>
              </li>
            ))}
          </ol>
        )}
      </article>
    </section>
  )
}
