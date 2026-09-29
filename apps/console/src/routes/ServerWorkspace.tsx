import { Link, Navigate, useParams } from 'react-router-dom'

import type { FleetReadModel, FleetServerReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'

function percent(value: number | undefined, unavailable: string) {
  return value === undefined ? unavailable : Math.round(value) + '%'
}

function resourceValue(server: FleetServerReadModel, kind: 'cpu' | 'memory' | 'filesystem', unavailable: string, loadLabel: string) {
  const resources = server.resources
  if (!resources) return unavailable
  if (kind === 'cpu') {
    if (resources.cpu.status !== 'available') return unavailable
    return resources.cpu.usagePercent === undefined
      ? resources.cpu.load1m === undefined ? unavailable : loadLabel + ' ' + resources.cpu.load1m.toFixed(2)
      : percent(resources.cpu.usagePercent, unavailable)
  }
  const item = kind === 'memory' ? resources.memory : resources.filesystem
  return item.status === 'available' ? percent(item.usedPercent, unavailable) : unavailable
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
  const { t, number, dateTime } = useI18n()
  const { instanceId } = useParams()
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
          <p className="eyebrow">{t('server.eyebrow')}</p>
          <h2 id="server-title">{server.displayName}</h2>
          <p className="muted">{server.origin}</p>
        </div>
        <span className={'status fleet-status-' + server.freshness}>{server.freshness}</span>
      </div>

      {server.connectivity !== 'live' ? (
        <div className="attention-strip" role="status">
          {t('server.cached')} {server.connectivity}.
          {server.staleReason ? ' ' + server.staleReason + '.' : ''}
        </div>
      ) : null}

      <div className="fleet-summary" aria-label={t('server.summary')}>
        <article className="card"><span>{t('server.version')}</span><strong>{server.version ?? t('common.unavailable')}</strong></article>
        <article className="card"><span>{t('server.agents')}</span><strong>{number(server.activeAgentCount)}</strong></article>
        <article className="card"><span>{t('server.runningTasks')}</span><strong>{number(server.taskCounts.inProgress)}</strong></article>
        <article className="card"><span>{t('server.blocked')}</span><strong>{number(server.taskCounts.blocked)}</strong></article>
        <article className="card"><span>{t('server.alerts')}</span><strong>{number(server.communication.alerts)}</strong></article>
        <article className="card"><span>{t('server.replies')}</span><strong>{number(server.communication.replyRequired)}</strong></article>
      </div>

      <article className="panel">
        <div>
          <p className="eyebrow">{t('server.hostResources')}</p>
          <h3>{t('server.lastTelemetry')}</h3>
        </div>
        <dl className="resource-grid">
          <div><dt>{t('common.cpu')}</dt><dd>{resourceValue(server, 'cpu', t('common.unavailable'), t('fleet.load'))}</dd></div>
          <div><dt>{t('common.ram')}</dt><dd>{resourceValue(server, 'memory', t('common.unavailable'), t('fleet.load'))}</dd></div>
          <div><dt>{t('common.disk')}</dt><dd>{resourceValue(server, 'filesystem', t('common.unavailable'), t('fleet.load'))}</dd></div>
        </dl>
        <p className="muted">
          {server.lastSeenAt ? t('server.lastActivity') + ' ' + dateTime(server.lastSeenAt) : t('server.noActivityTimestamp')}
        </p>
      </article>

      <article className="panel">
        <div>
          <p className="eyebrow">{t('server.activeSessions')}</p>
          <h3>{t('server.agentsHere')}</h3>
        </div>
        {agents.length ? (
          <div className="task-list">
            {agents.map((agent) => {
              const claimed = tasks.find((task) => task.active && task.owner?.agentName === agent.name)
              return (
                <article className="card" key={agent.name}>
                  <div className="section-heading">
                    <strong>{agent.name}</strong>
                    <span className="chip">{t('server.step')} {number(agent.currentStep)}</span>
                  </div>
                  <p>{agent.intent || t('server.noIntent')}</p>
                  <p className="muted">
                    {t('server.session')} {duration(agent.sessionAgeSeconds)} · {t('server.idle')} {duration(agent.idleSeconds)}
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
        ) : <p className="muted">{t('server.noActiveSessions')}</p>}
      </article>

      <div className="server-actions" aria-label={t('server.navigation')}>
        <Link className="nav-link" to="/">{t('nav.backToFleet')}</Link>
        <Link className="nav-link" to={'/servers/' + server.instanceId + '/tasks'}>{t('nav.tasks')}</Link>
        <Link className="nav-link" to={'/activity?server=' + encodeURIComponent(server.instanceId)}>{t('nav.activity')}</Link>
      </div>
    </section>
  )
}
