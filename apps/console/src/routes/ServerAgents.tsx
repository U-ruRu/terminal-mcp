import { Link, Navigate, useParams } from 'react-router-dom'

import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'
import { activityRoute, agentRoute, agentsRoute, serverRoute, taskRoute } from '../navigation/routes'

function duration(seconds: number | undefined): string {
  if (seconds === undefined) return 'unknown'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

export function ServerAgents({ instances }: { instances: FleetInstanceView[] }) {
  const { t } = useI18n()
  const { instanceId = '', agentId } = useParams()
  const instance = instances.find((item) => item.profile.instanceId === instanceId)
  if (!instance) return <Navigate to="/agents" replace />

  const snapshot = instance.runtime.realtime?.snapshot
  const agents = snapshot?.agents ?? []
  const tasks = snapshot?.tasks ?? []
  const selected = agentId ? agents.find((agent) => agent.agentId === agentId) : undefined

  if (agentId && !selected) {
    return (
      <section className="stack" aria-labelledby="agent-missing-title">
        <div className="page-heading"><div>
          <p className="eyebrow">{instance.profile.displayName}</p>
          <h2 id="agent-missing-title">{t('agents.missingTitle')}</h2>
          <p className="muted">{t('agents.missingDescription')}</p>
        </div></div>
        <Link className="text-link" to={agentsRoute(instanceId)}>{t('agents.backToAgents')}</Link>
      </section>
    )
  }

  if (selected) {
    const relatedTasks = tasks.filter((task) =>
      task.owner?.agentId === selected.agentId ||
      (task.participants ?? []).some((participant) => participant.agentId === selected.agentId),
    )
    return (
      <section className="stack" aria-labelledby="agent-title">
        <div className="page-heading">
          <div>
            <p className="eyebrow">{instance.profile.displayName}</p>
            <h2 id="agent-title">{selected.name}</h2>
            <p className="muted">{t('agents.exactSession')} {selected.agentId}</p>
          </div>
          <span className={'status status-' + instance.runtime.status}>{selected.status}</span>
        </div>
        <article className="panel">
          <h3>{t('agents.currentSession')}</h3>
          <p>{selected.intent || t('agents.noIntent')}</p>
          <p className="muted">{t('agents.sessionLabel')} {duration(selected.sessionAgeSeconds)} · {t('agents.idleLabel')} {duration(selected.idleSeconds)}</p>
          {selected.taskSummary && <p className="muted">{selected.taskSummary}</p>}
        </article>
        <article className="panel">
          <h3>{t('agents.relatedTasks')}</h3>
          {relatedTasks.length ? (
            <div className="task-list">
              {relatedTasks.map((task) => (
                <Link className="text-link" key={task.key} to={taskRoute(instanceId, task.namespace, task.taskId)}>
                  {task.taskId} · {task.title}
                </Link>
              ))}
            </div>
          ) : <FeedbackState variant="empty" title={t('agents.noTaskClaims')} />}
        </article>
        <div className="server-actions">
          <Link className="nav-link" to={activityRoute(instanceId, { agentId: selected.agentId })}>{t('agents.activity')}</Link>
          <Link className="nav-link" to={agentsRoute(instanceId)}>{t('agents.allAgents')}</Link>
          <Link className="nav-link" to={serverRoute(instanceId)}>{t('agents.serverOverview')}</Link>
        </div>
      </section>
    )
  }

  return (
    <section className="stack" aria-labelledby="agents-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{instance.profile.displayName}</p>
          <h2 id="agents-title">{t('nav.agents')}</h2>
          <p className="muted">{t('agents.description')}</p>
        </div>
        <span className={'status status-' + instance.runtime.status}>{instance.runtime.status}</span>
      </div>
      <div className="task-list">
        {agents.map((agent) => (
          <article className="panel" key={agent.agentId ?? `${agent.name}:${agent.lastActivityAt}`}>
            <div className="section-heading">
              {agent.agentId ? (
                <Link className="text-link" to={agentRoute(instanceId, agent.agentId)}>{agent.name}</Link>
              ) : <strong>{agent.name}</strong>}
              <span className="chip">{agent.status}</span>
            </div>
            <p>{agent.intent || t('agents.noIntent')}</p>
            {!agent.agentId && <p className="muted">{t('agents.identityUnavailable')}</p>}
          </article>
        ))}
        {agents.length === 0 && <FeedbackState variant="empty" title={t('agents.empty')} />}
      </div>
    </section>
  )
}
