import { Link, Navigate, useParams } from 'react-router-dom'

import type { FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'
import { activityRoute, agentRoute, taskRoute } from '../navigation/routes'

const agentStateKeys: Record<string, MessageKey> = { active: 'agents.state.active' }
function localizedAgentState(t: (key: MessageKey) => string, state: string): string {
  return t(agentStateKeys[state] ?? 'common.unknown')
}

export function ServerAgents({ instances }: { instances: FleetInstanceView[] }) {
  const { t, locale } = useI18n()
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
          <span className={'status status-' + instance.runtime.status}>{localizedAgentState(t, selected.status)}</span>
        </div>
        <article className="panel">
          <h3>{t('agents.currentSession')}</h3>
          <p>{selected.intent || t('agents.noIntent')}</p>
          <p className="muted">{t('agents.sessionLabel')} {formatDuration(selected.sessionAgeSeconds, locale, t('common.unknown'))} · {t('agents.idleLabel')} {formatDuration(selected.idleSeconds, locale, t('common.unknown'))}</p>
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
        </div>
      </section>
    )
  }

  return (
    <section className="stack" aria-label={t('nav.agents')}>
      <p className="muted page-supporting-copy">{t('agents.description')}</p>
      <div className="task-list">
        {agents.map((agent) => (
          <article className="panel" key={agent.agentId ?? `${agent.name}:${agent.lastActivityAt}`}>
            <div className="section-heading">
              {agent.agentId ? (
                <Link className="text-link" to={agentRoute(instanceId, agent.agentId)}>{agent.name}</Link>
              ) : <strong>{agent.name}</strong>}
              <span className="chip">{localizedAgentState(t, agent.status)}</span>
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
