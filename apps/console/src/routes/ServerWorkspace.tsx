import { useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import { LocalDiagnostics } from '../components/LocalDiagnostics'
import { ServerCard } from '../components/ServerCard'
import { FeedbackState } from '../components/UiPrimitives'
import type { ManagedFleetControlReadModel } from '../api/models'
import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import { isFleetControlRevisionRegression, loadCachedFleetControlForProfile, propagateCachedFleetControl, saveCachedFleetControl, type FleetControlFreshness } from '../connections/controlState'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { agentRoute, meshRoute, taskRoute } from '../navigation/routes'



export function ServerWorkspace({
  model,
  instances,
  loadFleetControl,
  diagnostics,
}: {
  model: FleetReadModel
  instances: FleetInstanceView[]
  loadFleetControl?: (instanceId: string) => Promise<ManagedFleetControlReadModel>
  diagnostics?: BrowserDiagnosticJournal
}) {
  const { t, number, dateTime, locale } = useI18n()
  const { instanceId } = useParams()
  const server = model.servers.find((item) => item.instanceId === instanceId)
  const instance = instances.find((item) => item.profile.instanceId === instanceId)
  const [controlState, setControlState] = useState<{
    instanceId: string
    control?: ManagedFleetControlReadModel
    unavailable?: boolean
    freshness: FleetControlFreshness
  }>(() => {
    if (!instanceId) return { instanceId: '', freshness: 'unknown' }
    const cached = loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)
    return cached
      ? { instanceId, control: cached.control, freshness: 'stale' }
      : { instanceId, freshness: 'unknown' }
  })
  useEffect(() => {
    if (!instanceId || !loadFleetControl) return
    let cancelled = false
    void loadFleetControl(instanceId)
      .then((control) => {
        if (cancelled) return
        const cached = loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)
        if (isFleetControlRevisionRegression(control, cached?.control)) {
          setControlState({
            instanceId,
            control: cached?.control,
            unavailable: !cached,
            freshness: cached ? 'stale' : 'unknown',
          })
          return
        }
        const observedAt = Date.now()
        saveCachedFleetControl(instanceId, control, observedAt)
        propagateCachedFleetControl(control, observedAt)
        const resolved = loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)
        const selected = resolved?.control ?? control
        const selectedIsDirect = (
          selected.fleetId === control.fleetId
          && selected.controlNodeId === control.controlNodeId
          && selected.nodeId === control.nodeId
          && selected.revisions.topology === control.revisions.topology
        )
        setControlState({
          instanceId,
          control: selected,
          freshness: selectedIsDirect ? 'fresh' : 'stale',
        })
      })
      .catch(() => {
        if (cancelled) return
        const cached = loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)
        setControlState({
          instanceId,
          control: cached?.control,
          unavailable: !cached,
          freshness: cached ? 'stale' : 'unknown',
        })
      })
    return () => { cancelled = true }
  }, [instance?.profile.origin, instanceId, loadFleetControl])

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
          {controlState.freshness === 'stale' ? ' · ' + t('connections.stale') : ''}
        </span>
      </div>

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
                    {t('server.session')} {formatDuration(agent.sessionAgeSeconds, locale, t('common.unknown'))} · {t('server.idle')} {formatDuration(agent.idleSeconds, locale, t('common.unknown'))}
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

      <LocalDiagnostics server={server} diagnostics={diagnostics} />
    </section>
  )
}
