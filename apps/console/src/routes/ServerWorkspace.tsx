import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import type { FleetReadModel, FleetServerReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'

function percent(value: number | undefined) {
  return value === undefined ? 'Unavailable' : Math.round(value) + '%'
}

function resourceValue(server: FleetServerReadModel, kind: 'cpu' | 'memory' | 'filesystem') {
  const resources = server.resources
  if (!resources) return 'Unavailable'
  if (kind === 'cpu') {
    if (resources.cpu.status !== 'available') return 'Unavailable'
    return resources.cpu.usagePercent === undefined
      ? resources.cpu.load1m === undefined ? 'Unavailable' : 'Load ' + resources.cpu.load1m.toFixed(2)
      : percent(resources.cpu.usagePercent)
  }
  const item = kind === 'memory' ? resources.memory : resources.filesystem
  return item.status === 'available' ? percent(item.usedPercent) : 'Unavailable'
}

function duration(seconds: number | undefined): string {
  if (seconds === undefined) return 'unknown'
  if (seconds < 60) return seconds + 's'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return minutes + 'm'
  return Math.floor(minutes / 60) + 'h ' + (minutes % 60) + 'm'
}

export function ServerWorkspace({
  model,
  instances,
}: {
  model: FleetReadModel
  instances: FleetInstanceView[]
}) {
  const { instanceId } = useParams()
  const navigate = useNavigate()
  const server = model.servers.find((item) => item.instanceId === instanceId)
  const instance = instances.find((item) => item.profile.instanceId === instanceId)
  if (!server) return <Navigate to="/" replace />

  const snapshot = instance?.runtime.realtime?.snapshot
  const agents = snapshot?.agents.filter((agent) => agent.status === 'active') ?? []
  const tasks = snapshot?.tasks ?? []

  return (
    <section className="stack" aria-labelledby="server-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Terminal MCP server</p>
          <h2 id="server-title">{server.displayName}</h2>
          <p className="muted">{server.origin}</p>
        </div>
        <div className="page-tools">
          <label className="server-switcher"><span>Server</span><select aria-label="Switch server" value={server.instanceId} onChange={(event) => navigate('/servers/' + encodeURIComponent(event.target.value))}>{model.servers.map((item) => <option key={item.instanceId} value={item.instanceId}>{item.displayName}</option>)}</select></label>
          <span className={'status fleet-status-' + server.freshness}>{server.freshness}</span>
        </div>
      </div>

      {server.connectivity !== 'live' ? (
        <div className="attention-strip" role="status">
          Showing the last cached snapshot. Live connection is {server.connectivity}.
          {server.staleReason ? ' ' + server.staleReason + '.' : ''}
        </div>
      ) : null}

      <div className="fleet-summary" aria-label="Server summary">
        <article className="card"><span>Version</span><strong>{server.version ?? 'Unavailable'}</strong></article>
        <article className="card"><span>Agents</span><strong>{server.activeAgentCount}</strong></article>
        <article className="card"><span>Running tasks</span><strong>{server.taskCounts.inProgress}</strong></article>
        <article className="card"><span>Blocked</span><strong>{server.taskCounts.blocked}</strong></article>
        <article className="card"><span>Alerts</span><strong>{server.communication.alerts}</strong></article>
        <article className="card"><span>Replies</span><strong>{server.communication.replyRequired}</strong></article>
      </div>

      <article className="panel">
        <div>
          <p className="eyebrow">Host resources</p>
          <h3>Last reported telemetry</h3>
        </div>
        <dl className="resource-grid">
          <div><dt>CPU</dt><dd>{resourceValue(server, 'cpu')}</dd></div>
          <div><dt>RAM</dt><dd>{resourceValue(server, 'memory')}</dd></div>
          <div><dt>Disk</dt><dd>{resourceValue(server, 'filesystem')}</dd></div>
        </dl>
        <p className="muted">
          {server.lastSeenAt ? 'Last activity ' + server.lastSeenAt : 'No activity timestamp available.'}
        </p>
      </article>

      <article className="panel">
        <div>
          <p className="eyebrow">Active sessions</p>
          <h3>Agents on this server</h3>
        </div>
        {agents.length ? (
          <div className="task-list">
            {agents.map((agent) => {
              const claimed = tasks.find((task) => task.active && task.owner?.agentName === agent.name)
              return (
                <article className="card" key={agent.name}>
                  <div className="section-heading">
                    <strong>{agent.name}</strong>
                    <span className="chip">step {agent.currentStep}</span>
                  </div>
                  <p>{agent.intent || 'No current intent.'}</p>
                  <p className="muted">
                    Session {duration(agent.sessionAgeSeconds)} · idle {duration(agent.idleSeconds)}
                    {' · '}{agent.lastActivity}
                  </p>
                  {claimed && (
                    <Link className="text-link" to={
                      '/servers/' + encodeURIComponent(server.instanceId) + '/tasks/' +
                      encodeURIComponent(claimed.namespace) + '/' + encodeURIComponent(claimed.taskId)
                    }>
                      {claimed.taskId} · {claimed.title}
                    </Link>
                  )}
                  {!claimed && agent.taskSummary && <p className="muted">{agent.taskSummary}</p>}
                </article>
              )
            })}
          </div>
        ) : server.activeIntents.length ? (
          <ul className="intent-list">
            {server.activeIntents.map((intent) => <li key={intent}>{intent}</li>)}
          </ul>
        ) : <p className="muted">No active sessions in the cached snapshot.</p>}
      </article>

      <div className="server-actions" aria-label="Server navigation">
        <Link className="nav-link" to="/">Back to fleet</Link>
        <Link className="nav-link" to={'/servers/' + server.instanceId + '/tasks'}>Tasks</Link>
        <Link className="nav-link" to={'/activity?server=' + encodeURIComponent(server.instanceId)}>Activity</Link>
      </div>
    </section>
  )
}
