import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import type { TaskReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'
import { activityRoute, agentRoute, serverRoute, taskRoute } from '../navigation/routes'

export type ServerTaskLoader = (
  instanceId: string,
  namespace: string,
  taskId: string,
) => Promise<TaskReadModel>

type StateFilter = 'open' | TaskReadModel['state'] | 'all'
type OperationalFilter = TaskReadModel['operationalStatus'] | 'all'

export function ServerTasks({
  instances,
  loadTask,
}: {
  instances: FleetInstanceView[]
  loadTask?: ServerTaskLoader
}) {
  const { t } = useI18n()
  const { instanceId = '', namespace, taskId } = useParams()
  const navigate = useNavigate()
  const instance = instances.find((item) => item.profile.instanceId === instanceId)
  const tasks = useMemo(
    () => instance?.runtime.realtime?.snapshot?.tasks ?? [],
    [instance],
  )
  const namespaces = useMemo(
    () => Array.from(new Set(tasks.map((task) => task.namespace))).sort(),
    [tasks],
  )
  const [namespaceFilter, setNamespaceFilter] = useState('all')
  const [stateFilter, setStateFilter] = useState<StateFilter>('open')
  const [operationalFilter, setOperationalFilter] = useState<OperationalFilter>('all')
  const filteredTasks = useMemo(() => tasks.filter((task) => {
    if (namespaceFilter !== 'all' && task.namespace !== namespaceFilter) return false
    if (stateFilter === 'open') {
      if (task.state === 'done' || task.archived) return false
    } else if (stateFilter !== 'all' && task.state !== stateFilter) return false
    return operationalFilter === 'all' || task.operationalStatus === operationalFilter
  }), [namespaceFilter, operationalFilter, stateFilter, tasks])

  const cached = namespace && taskId
    ? tasks.find((task) => task.namespace === namespace && task.taskId === taskId)
    : undefined
  const requestedKey = namespace && taskId ? namespace + '/' + taskId : ''
  const isDetailRoute = requestedKey !== ''
  const [loaded, setLoaded] = useState<{ key: string; task?: TaskReadModel; error?: string }>({ key: '' })
  const detail = loaded.key === requestedKey && loaded.task ? loaded.task : cached
  const error = loaded.key === requestedKey ? (loaded.error ?? '') : ''

  useEffect(() => {
    if (!instance || !namespace || !taskId || !loadTask) return
    const key = namespace + '/' + taskId
    let cancelled = false
    void loadTask(instanceId, namespace, taskId)
      .then((task) => { if (!cancelled) setLoaded({ key, task }) })
      .catch((reason: unknown) => {
        if (!cancelled) setLoaded({ key, error: reason instanceof Error ? reason.message : 'task_load_failed' })
      })
    return () => { cancelled = true }
  }, [instance, instanceId, loadTask, namespace, taskId])

  if (!instance) return <Navigate to="/" replace />

  return (
    <section className="stack" aria-labelledby="server-tasks-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{instance.profile.displayName}</p>
          <h2 id="server-tasks-title">{t('nav.tasks')}</h2>
          <p className="muted">{t('tasks.description')}</p>
        </div>
        <div className="page-tools">
          <label className="server-switcher"><span>{t('server.switch')}</span><select aria-label={t('aria.switchServer')} value={instanceId} onChange={(event) => navigate('/servers/' + encodeURIComponent(event.target.value) + '/tasks')}>{instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}</select></label>
          <span className={'status status-' + instance.runtime.status}>{instance.runtime.status}</span>
        </div>
      </div>

      {instance.runtime.status !== 'live' && (
        <div className="attention-strip" role="status">
          {t('tasks.cached')} {instance.runtime.status}.
        </div>
      )}

      {isDetailRoute ? (
        detail ? (
          <article className="panel" aria-label={t('title.taskDetail')}>
            <div className="section-heading">
              <div>
                <p className="eyebrow">{detail.namespace}</p>
                <h3>{detail.taskId} · {detail.title}</h3>
              </div>
              <span className="chip">{detail.operationalStatus}</span>
            </div>
            <p>{detail.nextAction || t('tasks.noNextAction')}</p>
            <div className="chip-row">
              <span className="chip">{detail.priority}</span>
              <span className="chip">{detail.lane}</span>
              <span className="chip">{detail.state}</span>
              {detail.owner?.agentId ? <Link className="text-link" to={agentRoute(instanceId, detail.owner.agentId)}>{t('common.owner')} {detail.owner.agentName}</Link> : detail.owner ? <span className="chip">{t('common.owner')} {detail.owner.agentName}</span> : null}
              {(detail.participants ?? []).map((participant) => participant.agentId ? <Link className="text-link" key={participant.agentId} to={agentRoute(instanceId, participant.agentId)}>{t('common.agent')} {participant.agentName}</Link> : <span className="chip" key={`${participant.agentName}:${participant.claimedAt}`}>{t('common.agent')} {participant.agentName}</span>)}
              {detail.candidateRef && <span className="chip">{t('common.candidate')} {detail.candidateRef.slice(0, 12)}</span>}
            </div>
            {error && <p className="muted">{t('tasks.refreshFailed')} {error}. {t('tasks.cachedRemains')}</p>}
          </article>
        ) : (
          <FeedbackState
            variant={error ? 'error' : 'loading'}
            title={error ? t('tasks.refreshFailed') : t('tasks.loadingDetail')}
            detail={error || undefined}
          />
        )
      ) : (
        <>
          <div className="page-tools task-filters" aria-label={t('tasks.filters')}>
            <label className="server-switcher ui-field"><span>{t('tasks.namespace')}</span><select aria-label={t('tasks.namespace')} value={namespaceFilter} onChange={(event) => setNamespaceFilter(event.target.value)}><option value="all">{t('tasks.allNamespaces')}</option>{namespaces.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
            <label className="server-switcher ui-field"><span>{t('tasks.state')}</span><select aria-label={t('tasks.state')} value={stateFilter} onChange={(event) => setStateFilter(event.target.value as StateFilter)}><option value="open">{t('tasks.open')}</option><option value="ready">ready</option><option value="in_progress">in_progress</option><option value="blocked">blocked</option><option value="deferred">deferred</option><option value="done">{t('tasks.completed')}</option><option value="all">{t('tasks.all')}</option></select></label>
            <label className="server-switcher ui-field"><span>{t('tasks.operationalStatus')}</span><select aria-label={t('tasks.operationalStatus')} value={operationalFilter} onChange={(event) => setOperationalFilter(event.target.value as OperationalFilter)}><option value="all">{t('tasks.all')}</option><option value="ready">ready</option><option value="in_progress">in_progress</option><option value="blocked">blocked</option><option value="deferred">deferred</option><option value="done">done</option></select></label>
          </div>
          <div className="task-list" aria-label={t('tasks.serverTasks')}>
            {filteredTasks.map((task) => (
              <Link className="panel task-card-link" key={task.key} to={taskRoute(instanceId, task.namespace, task.taskId)} aria-label={`${task.taskId} · ${task.title}`}>
                <div className="section-heading">
                  <div><strong>{task.taskId} · {task.title}</strong><p className="muted">{task.namespace} · {task.priority} · {task.lane}</p></div>
                  <span className="chip">{task.operationalStatus}</span>
                </div>
                <p>{task.nextAction || t('tasks.noNextAction')}</p>
                <span className="text-link">{t('tasks.openDetail')}</span>
              </Link>
            ))}
            {filteredTasks.length === 0 && <FeedbackState variant="empty" title={t('tasks.noMatchingTasks')} />}
          </div>
        </>
      )}

      <div className="server-actions">
        <Link className="nav-link" to={isDetailRoute ? `/servers/${encodeURIComponent(instanceId)}/tasks` : serverRoute(instanceId)}>{isDetailRoute ? t('nav.backToTasks') : t('nav.backToServer')}</Link>
        <Link className="nav-link" to={activityRoute(instanceId)}>{t('nav.activity')}</Link>
      </div>
    </section>
  )
}
