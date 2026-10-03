import { useMemo, useRef, useState } from 'react'

import type { FleetReadModel } from '../fleet/readModel'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { ServerCard } from './ServerCard'
import { FeedbackState, SegmentedControl } from './UiPrimitives'
import { needsAttention, serverAlphaSort, serverProblemSort, serverVisualState } from './serverPresentation'

type FleetFilter = 'all' | 'attention' | 'live'

export function FleetDashboard({ model }: { model: FleetReadModel }) {
  const { t, number, locale } = useI18n()
  const [filter, setFilter] = useState<FleetFilter>('all')
  const serverListRef = useRef<HTMLDivElement>(null)
  const states = model.servers.map(serverVisualState)
  const problemCount = model.servers.filter(needsAttention).length
  const criticalCount = states.filter((state) => state === 'offline' || state === 'critical').length
  const resourceAttentionCount = states.filter((state) => state === 'attention').length
  const staleCount = states.filter((state) => state === 'stale').length
  const fleetState = criticalCount > 0
    ? 'partial'
    : resourceAttentionCount > 0 || staleCount > 0
      ? 'attention'
      : states.some((state) => state === 'loading')
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
      <div className="page-heading fleet-heading">
        <h2 id="fleet-title">{t('fleet.title')}</h2>
      </div>

      <button
        type="button"
        className={'fleet-status-card fleet-status-card-' + fleetState}
        aria-label={
          fleetState === 'partial'
            ? t('fleet.partialOffline') + ': ' + number(problemCount) + ' ' + t('fleet.servers')
            : fleetState === 'attention'
              ? t('fleet.needsAttention') + ': ' + number(problemCount) + ' ' + t('fleet.servers')
              : fleetState === 'loading'
                ? t('status.catchingUp')
                : t('fleet.live') + ': ' + number(model.summary.totalServers) + ' ' + t('fleet.servers')
        }
        onClick={() => focusServerList(fleetState === 'attention' || fleetState === 'partial' ? 'attention' : 'all')}
      >
        <span className="fleet-status-icon" aria-hidden="true">{fleetState === 'healthy' ? '✓' : fleetState === 'loading' ? '…' : '!'}</span>
        <span>
          <strong>{fleetState === 'partial' ? t('fleet.partialOffline') : fleetState === 'attention' ? t('fleet.needsAttention') : fleetState === 'loading' ? t('status.catchingUp') : t('fleet.live')}</strong>
          <small>
            {number(model.summary.totalServers)} {t('fleet.servers')}
            {' · '}{number(criticalCount)} {t('fleet.offline')}
            {' · '}{number(problemCount)} {t('fleet.needsAttention')}
            {' · '}{number(staleCount)} {t('fleet.stale')}
          </small>
        </span>
        <span className="fleet-status-chevron" aria-hidden="true">›</span>
      </button>

      <SegmentedControl
        label={t('fleet.filterServers')}
        value={filter}
        onChange={(value) => setFilter(value as FleetFilter)}
        options={[
          { value: 'all', label: t('common.all') },
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
          <FeedbackState variant="empty" title={t('fleet.noActiveSharedSessions')} />
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
                  {t('fleet.age')} {formatDuration(session.sessionAgeSeconds, locale)} · {t('fleet.remaining')}{' '}
                  {formatDuration(session.sessionRemainingSeconds, locale)}
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
          <FeedbackState variant="empty" title={t('fleet.noRecentActivity')} />
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
