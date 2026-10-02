import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import { ResourceMetric } from '../components/ResourceMetric'
import { StatusBadge } from '../components/StatusBadge'
import { serverVisualState, type ServerVisualState } from '../components/serverPresentation'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { activityRoute, agentRoute, contextRoute, healthRoute, taskRoute, tasksRoute } from '../navigation/routes'


function statusLabel(state: ServerVisualState, t: ReturnType<typeof useI18n>['t']): string {
  if (state === 'healthy') return t('status.live')
  if (state === 'stale') return t('status.stale')
  if (state === 'offline') return t('status.offline')
  if (state === 'loading') return t('status.catchingUp')
  return t('fleet.needsAttention')
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
  const navigate = useNavigate()
  const server = model.servers.find((item) => item.instanceId === instanceId)
  const instance = instances.find((item) => item.profile.instanceId === instanceId)
  if (!server) return <Navigate to="/" replace />

  const visualState = serverVisualState(server)
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
        <div className="page-tools">
          <label className="server-switcher"><span>{t('server.switch')}</span><select aria-label={t('aria.switchServer')} value={server.instanceId} onChange={(event) => navigate('/servers/' + encodeURIComponent(event.target.value))}>{model.servers.map((item) => <option key={item.instanceId} value={item.instanceId}>{item.displayName}</option>)}</select></label>
          <StatusBadge state={visualState} label={statusLabel(visualState, t)} />
        </div>
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
          <ResourceMetric server={server} kind="cpu" label={t('common.cpu')} />
          <ResourceMetric server={server} kind="memory" label={t('common.ram')} />
          <ResourceMetric server={server} kind="filesystem" label={t('common.disk')} />
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
              const claimed = agent.agentId
                ? tasks.find((task) => task.active && task.owner?.agentId === agent.agentId)
                : undefined
              return (
                <article className="card" key={agent.agentId ?? `${agent.name}:${agent.lastActivityAt}`}>
                  <div className="section-heading">
                    {agent.agentId ? <Link className="text-link" to={agentRoute(server.instanceId, agent.agentId)}>{agent.name}</Link> : <strong>{agent.name}</strong>}
                    <span className="chip">{t('server.step')} {number(agent.currentStep)}</span>
                  </div>
                  <p>{agent.intent || t('server.noIntent')}</p>
                  <p className="muted">
                    {t('server.session')} {duration(agent.sessionAgeSeconds)} · {t('server.idle')} {duration(agent.idleSeconds)}
                    {' · '}{agent.lastActivity}
                  </p>
                  {claimed && (
                    <Link className="text-link" to={taskRoute(server.instanceId, claimed.namespace, claimed.taskId)}>
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
        <Link className="nav-link" to={tasksRoute(server.instanceId)}>{t('nav.tasks')}</Link>
        <Link className="nav-link" to={activityRoute(server.instanceId)}>{t('nav.activity')}</Link>
        <Link className="nav-link" to={contextRoute(server.instanceId)}>{t('nav.context')}</Link>
        <Link className="nav-link" to={healthRoute(server.instanceId)}>{t('nav.health')}</Link>
      </div>
    </section>
  )
}
