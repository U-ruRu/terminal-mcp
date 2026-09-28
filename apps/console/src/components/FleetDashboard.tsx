import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import type { FleetReadModel, FleetServerReadModel } from '../fleet/readModel'

type FleetFilter = 'all' | 'attention' | 'live'

function percent(value: number | undefined): string {
  return value === undefined ? 'Unavailable' : Math.round(value) + '%'
}

function resourceValue(server: FleetServerReadModel, kind: 'cpu' | 'memory' | 'filesystem') {
  const resources = server.resources
  if (!resources) return 'Unavailable'
  if (kind === 'cpu') {
    if (resources.cpu.status !== 'available') return 'Unavailable'
    if (resources.cpu.usagePercent !== undefined) return percent(resources.cpu.usagePercent)
    return resources.cpu.load1m === undefined
      ? 'Unavailable'
      : 'Load ' + resources.cpu.load1m.toFixed(2)
  }
  const item = kind === 'memory' ? resources.memory : resources.filesystem
  return item.status === 'available' ? percent(item.usedPercent) : 'Unavailable'
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

function lastHealth(server: FleetServerReadModel): string {
  if (!server.snapshotAvailable) return 'No snapshot'
  if (!server.lastSeenAt) return 'Last health unavailable'
  return 'Last health ' + server.lastSeenAt.replace('T', ' ').replace('Z', ' UTC')
}

export function FleetDashboard({ model }: { model: FleetReadModel }) {
  const [filter, setFilter] = useState<FleetFilter>('all')
  const servers = useMemo(() => {
    if (filter === 'attention') return model.servers.filter(needsAttention)
    if (filter === 'live') return model.servers.filter((server) => server.connectivity === 'live')
    return model.servers
  }, [filter, model.servers])

  return (
    <section className="stack" aria-labelledby="fleet-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Terminal MCP fleet</p>
          <h2 id="fleet-title">Fleet overview</h2>
          <p className="muted">Health, work pressure and recent activity across paired servers.</p>
        </div>
        <span className="environment-badge">{model.summary.totalServers} servers</span>
      </div>

      <div className="fleet-summary" aria-label="Fleet totals">
        <article className="card"><span>Live</span><strong>{model.summary.liveServers}</strong></article>
        <article className="card"><span>Stale</span><strong>{model.summary.staleServers}</strong></article>
        <article className="card"><span>Offline</span><strong>{model.summary.offlineServers}</strong></article>
        <article className="card"><span>Active agents</span><strong>{model.summary.activeAgents}</strong></article>
        <article className="card"><span>Blocked tasks</span><strong>{model.summary.blockedTasks}</strong></article>
        <article className="card"><span>Alerts</span><strong>{model.summary.alerts}</strong></article>
      </div>

      <div className="fleet-filter" role="group" aria-label="Filter servers">
        <button type="button" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>
          All ({model.summary.totalServers})
        </button>
        <button
          type="button"
          aria-pressed={filter === 'attention'}
          onClick={() => setFilter('attention')}
        >
          Needs attention
        </button>
        <button type="button" aria-pressed={filter === 'live'} onClick={() => setFilter('live')}>
          Live
        </button>
      </div>

      <div className="fleet-grid">
        {servers.map((server) => (
          <article
            className={'fleet-server fleet-server-' + server.freshness}
            key={server.instanceId}
            aria-label={server.displayName + ' server'}
          >
            <div className="fleet-server-heading">
              <div>
                <p className="eyebrow">{server.version ?? 'Version unavailable'}</p>
                <h3>{server.displayName}</h3>
                <p className="muted">{server.origin}</p>
              </div>
              <span className={'status fleet-status-' + server.freshness}>{server.freshness}</span>
            </div>

            <p className="muted">{lastHealth(server)}</p>

            <dl className="resource-grid">
              <div><dt>CPU</dt><dd>{resourceValue(server, 'cpu')}</dd></div>
              <div><dt>RAM</dt><dd>{resourceValue(server, 'memory')}</dd></div>
              <div><dt>Disk</dt><dd>{resourceValue(server, 'filesystem')}</dd></div>
            </dl>

            <div className="fleet-pressure">
              <span>{server.activeAgentCount} agents</span>
              <span>{server.taskCounts.inProgress} running</span>
              <span>{server.taskCounts.ready} ready</span>
              <span>{server.taskCounts.blocked} blocked</span>
            </div>

            {server.activeIntents.length > 0 ? (
              <ul className="intent-list" aria-label={server.displayName + ' active intents'}>
                {server.activeIntents.slice(0, 3).map((intent) => <li key={intent}>{intent}</li>)}
              </ul>
            ) : (
              <p className="muted">No active intents.</p>
            )}

            <Link className="server-open-link" to={'/servers/' + server.instanceId}>
              Open server
            </Link>

            {needsAttention(server) ? (
              <div className="attention-strip">
                {server.communication.alerts > 0 ? <span>{server.communication.alerts} alert</span> : null}
                {server.communication.replyRequired > 0 ? <span>{server.communication.replyRequired} reply</span> : null}
                {server.blockerCount > 0 ? <span>{server.blockerCount} blocker</span> : null}
                {server.lastError ? <span>{server.lastError}</span> : null}
                {server.staleReason ? <span>{server.staleReason}</span> : null}
              </div>
            ) : null}
          </article>
        ))}
      </div>

      <article className="panel">
        <div>
          <p className="eyebrow">Recent activity</p>
          <h3>Across the fleet</h3>
        </div>
        {model.recentActivity.length === 0 ? (
          <p className="muted">No recent activity.</p>
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
