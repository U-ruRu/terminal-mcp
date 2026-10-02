import { useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import { ServerCard } from '../components/ServerCard'
import { FeedbackState } from '../components/UiPrimitives'
import type { ManagedFleetControlReadModel } from '../api/models'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { agentRoute, meshRoute, taskRoute } from '../navigation/routes'


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
  loadFleetControl,
}: {
  model: FleetReadModel
  instances: FleetInstanceView[]
  loadFleetControl?: (instanceId: string) => Promise<ManagedFleetControlReadModel>
}) {
  const { t, number, dateTime } = useI18n()
  const { instanceId } = useParams()
  const [controlState, setControlState] = useState<{ instanceId: string; control?: ManagedFleetControlReadModel; unavailable?: boolean }>({ instanceId: '' })
  const server = model.servers.find((item) => item.instanceId === instanceId)
  const instance = instances.find((item) => item.profile.instanceId === instanceId)
  useEffect(() => {
    if (!instanceId || !loadFleetControl) return
    let cancelled = false
    void loadFleetControl(instanceId)
      .then((control) => { if (!cancelled) setControlState({ instanceId, control }) })
      .catch(() => { if (!cancelled) setControlState({ instanceId, unavailable: true }) })
    return () => { cancelled = true }
  }, [instanceId, loadFleetControl])

  if (!server) return <Navigate to="/" replace />

  const snapshot = instance?.runtime.realtime?.snapshot
  const agents = snapshot?.agents.filter((agent) => agent.status === 'active') ?? []
  const tasks = snapshot?.tasks ?? []
  const control = controlState.instanceId === server.instanceId ? controlState.control : undefined
  const localNode = control?.managed ? control.nodes.find((node) => node.nodeId === control.nodeId && node.state !== 'detached') : undefined
  const mesh = localNode?.meshId ? control?.meshes.find((item) => item.meshId === localNode.meshId) : undefined
  const membershipKnown = controlState.instanceId === server.instanceId && (Boolean(control) || Boolean(controlState.unavailable))

  return (
    <section className="stack" aria-label={t('title.server')}>
      <div className="server-entity-context">
        <p className="muted server-origin">{server.origin}</p>
        <span className="server-membership">
          {t('connections.mesh')}: {' '}
          {!membershipKnown ? t('status.catchingUp') : controlState.unavailable ? t('connections.unknown') : mesh ? (
            <Link className="text-link" to={meshRoute(mesh.meshId)}>{mesh.displayName}</Link>
          ) : localNode || control?.managed === false ? t('connections.standalone') : t('connections.unknown')}
        </span>
      </div>

      {server.connectivity !== 'live' ? (
        <div className="attention-strip" role="status">
          {t('server.cached')} {server.connectivity === 'offline' ? t('status.offline') : server.freshness === 'stale' ? t('status.stale') : t('status.catchingUp')}.
        </div>
      ) : null}

      <ServerCard server={server} variant="large" interactive={false} />

      <p className="muted server-card-telemetry-time">
        {server.lastSeenAt ? t('server.lastActivity') + ' ' + dateTime(server.lastSeenAt) : t('server.noActivityTimestamp')}
      </p>

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
        ) : <FeedbackState variant="empty" title={t('server.noActiveSessions')} />}
      </article>

    </section>
  )
}
