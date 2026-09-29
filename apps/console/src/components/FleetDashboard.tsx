import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import type { FleetReadModel, FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'

type FleetFilter = 'all' | 'attention' | 'live'

function percent(value: number | undefined, unavailable: string): string {
  return value === undefined ? unavailable : Math.round(value) + '%'
}

function resourceValue(
  server: FleetServerReadModel,
  kind: 'cpu' | 'memory' | 'filesystem',
  unavailable: string,
  loadLabel: string,
) {
  const resources = server.resources
  if (!resources) return unavailable
  if (kind === 'cpu') {
    if (resources.cpu.status !== 'available') return unavailable
    if (resources.cpu.usagePercent !== undefined) return percent(resources.cpu.usagePercent, unavailable)
    return resources.cpu.load1m === undefined ? unavailable : loadLabel + ' ' + resources.cpu.load1m.toFixed(2)
  }
  const item = kind === 'memory' ? resources.memory : resources.filesystem
  return item.status === 'available' ? percent(item.usedPercent, unavailable) : unavailable
}

function needsAttention(server: FleetServerReadModel): boolean {
  return (
    server.connectivity !== 'live' ||
    server.blockerCount > 0 ||
    server.communication.alerts > 0 ||
    server.communication.replyRequired > 0 ||
    server.healthy === false
  )
}

export function FleetDashboard({ model }: { model: FleetReadModel }) {
  const { t, number } = useI18n()
  const [filter, setFilter] = useState<FleetFilter>('all')
  const servers = useMemo(() => {
    if (filter === 'attention') return model.servers.filter(needsAttention)
    if (filter === 'live') return model.servers.filter((server) => server.connectivity === 'live')
    return model.servers
  }, [filter, model.servers])

  const lastHealth = (server: FleetServerReadModel): string => {
    if (!server.snapshotAvailable) return t('fleet.noSnapshot')
    if (!server.lastSeenAt) return t('fleet.lastHealthUnavailable')
    return t('fleet.lastHealth') + ' ' + server.lastSeenAt.replace('T', ' ').replace('Z', ' UTC')
  }

  const freshness = (value: FleetServerReadModel['freshness']) => {
    if (value === 'fresh') return t('status.fresh')
    if (value === 'stale') return t('status.stale')
    return t('status.offline')
  }

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

      <div className="fleet-summary" aria-label={t('fleet.totals')}>
        <article className="card"><span>{t('fleet.live')}</span><strong>{number(model.summary.liveServers)}</strong></article>
        <article className="card"><span>{t('fleet.stale')}</span><strong>{number(model.summary.staleServers)}</strong></article>
        <article className="card"><span>{t('fleet.offline')}</span><strong>{number(model.summary.offlineServers)}</strong></article>
        <article className="card"><span>{t('fleet.activeAgents')}</span><strong>{number(model.summary.activeAgents)}</strong></article>
        <article className="card"><span>{t('fleet.blockedTasks')}</span><strong>{number(model.summary.blockedTasks)}</strong></article>
        <article className="card"><span>{t('fleet.alerts')}</span><strong>{number(model.summary.alerts)}</strong></article>
      </div>

      <div className="fleet-filter" role="group" aria-label={t('fleet.filterServers')}>
        <button type="button" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>
          {t('common.all')} ({number(model.summary.totalServers)})
        </button>
        <button type="button" aria-pressed={filter === 'attention'} onClick={() => setFilter('attention')}>
          {t('fleet.needsAttention')}
        </button>
        <button type="button" aria-pressed={filter === 'live'} onClick={() => setFilter('live')}>
          {t('fleet.live')}
        </button>
      </div>

      <div className="fleet-grid">
        {servers.map((server) => (
          <article
            className={'fleet-server fleet-server-' + server.freshness}
            key={server.instanceId}
            aria-label={server.displayName + ' ' + t('fleet.serverSuffix')}
          >
            <div className="fleet-server-heading">
              <div>
                <p className="eyebrow">{server.version ?? t('fleet.versionUnavailable')}</p>
                <h3>{server.displayName}</h3>
                <p className="muted">{server.origin}</p>
              </div>
              <span className={'status fleet-status-' + server.freshness}>{freshness(server.freshness)}</span>
            </div>

            <p className="muted">{lastHealth(server)}</p>

            <dl className="resource-grid">
              <div><dt>{t('common.cpu')}</dt><dd>{resourceValue(server, 'cpu', t('common.unavailable'), t('fleet.load'))}</dd></div>
              <div><dt>{t('common.ram')}</dt><dd>{resourceValue(server, 'memory', t('common.unavailable'), t('fleet.load'))}</dd></div>
              <div><dt>{t('common.disk')}</dt><dd>{resourceValue(server, 'filesystem', t('common.unavailable'), t('fleet.load'))}</dd></div>
            </dl>

            <div className="fleet-pressure">
              <span>{number(server.activeAgentCount)} {t('fleet.agents')}</span>
              <span>{number(server.taskCounts.inProgress)} {t('fleet.running')}</span>
              <span>{number(server.taskCounts.ready)} {t('fleet.ready')}</span>
              <span>{number(server.taskCounts.blocked)} {t('fleet.blocked')}</span>
            </div>

            {server.activeIntents.length > 0 ? (
              <ul className="intent-list" aria-label={server.displayName + ' ' + t('fleet.activeIntents')}>
                {server.activeIntents.slice(0, 3).map((intent) => <li key={intent}>{intent}</li>)}
              </ul>
            ) : (
              <p className="muted">{t('fleet.noActiveIntents')}</p>
            )}

            <Link className="server-open-link" to={'/servers/' + server.instanceId}>{t('fleet.openServer')}</Link>

            {needsAttention(server) ? (
              <div className="attention-strip">
                {server.communication.alerts > 0 ? <span>{number(server.communication.alerts)} {t('fleet.alert')}</span> : null}
                {server.communication.replyRequired > 0 ? <span>{number(server.communication.replyRequired)} {t('fleet.reply')}</span> : null}
                {server.blockerCount > 0 ? <span>{number(server.blockerCount)} {t('fleet.blocker')}</span> : null}
                {server.lastError ? <span>{server.lastError}</span> : null}
                {server.staleReason ? <span>{server.staleReason}</span> : null}
              </div>
            ) : null}
          </article>
        ))}
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
                <span>{t('fleet.origin')} {session.originInstanceId ?? t('common.unavailable')} · {session.attachments.length} {t('fleet.attached')}</span>
                <small>{t('fleet.age')} {session.sessionAgeSeconds ?? '—'}s · {t('fleet.remaining')} {session.sessionRemainingSeconds ?? '—'}s</small>
                <small>{session.attachments.map((item) => item.displayName + ': ' + item.intent).join(' · ')}</small>
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
