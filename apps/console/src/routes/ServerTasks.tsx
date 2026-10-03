import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useLocation, useParams } from 'react-router-dom'
import { Icon } from '../components/Icon'

import type { TaskReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'
import { returnToState } from '../navigation/context'
import { agentRoute, serverRoute, taskRoute } from '../navigation/routes'

export type ServerTaskLoader = (
  instanceId: string,
  namespace: string,
  taskId: string,
) => Promise<TaskReadModel>

type StateFilter = 'open' | TaskReadModel['state'] | 'all'
type OperationalFilter = TaskReadModel['operationalStatus'] | 'all'

function taskStatusLabel(
  value: TaskReadModel['state'] | TaskReadModel['operationalStatus'],
  t: ReturnType<typeof useI18n>['t'],
): string {
  if (value === 'ready') return t('tasks.ready')
  if (value === 'in_progress') return t('tasks.inProgress')
  if (value === 'blocked') return t('tasks.blocked')
  if (value === 'deferred') return t('tasks.deferred')
  return t('tasks.completed')
}


function runtimeStatusLabel(value: string, t: ReturnType<typeof useI18n>['t']): string {
  if (value === 'live') return t('status.live')
  if (value === 'offline') return t('status.offline')
  if (value === 'stale') return t('status.stale')
  if (value === 'connecting' || value === 'reconnecting' || value === 'catching_up') return t('status.catchingUp')
  return t('common.unknown')
}

export function ServerTasks({
  instances,
  loadTask,
}: {
  instances: FleetInstanceView[]
  loadTask?: ServerTaskLoader
}) {
  const { t } = useI18n()
  const location = useLocation()
  const { instanceId = '', namespace, taskId } = useParams()
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
    <section className="stack" aria-label={t('nav.tasks')}>
      <p className="muted page-supporting-copy">{t('tasks.description')}</p>

      <div className="operational-status-slot" aria-live="polite">
        {instance.runtime.status !== 'live' ? (
          <div className="attention-strip" role="status">
            {t('tasks.cached')} {runtimeStatusLabel(instance.runtime.status, t)}.
          </div>
        ) : null}
      </div>

      {isDetailRoute ? (
        detail ? (
          <article className="panel" aria-label={t('title.taskDetail')}>
            <div className="section-heading">
              <div>
                <p className="eyebrow">{detail.namespace}</p>
                <h3>{detail.taskId} · {detail.title}</h3>
              </div>
              <span className="chip">{taskStatusLabel(detail.operationalStatus, t)}</span>
            </div>
            <section className="task-next-action" aria-label={t('tasks.nextAction')}>
              <span>{t('tasks.nextAction')}</span>
              <strong>{detail.nextAction || t('tasks.noNextAction')}</strong>
            </section>
            <dl className="task-detail-grid">
              <div><dt>{t('tasks.state')}</dt><dd><span className="chip">{taskStatusLabel(detail.state, t)}</span></dd></div>
              <div><dt>{t('tasks.operationalStatus')}</dt><dd><span className="chip">{taskStatusLabel(detail.operationalStatus, t)}</span></dd></div>
              <div><dt>{t('tasks.priority')}</dt><dd>{detail.priority}</dd></div>
              <div><dt>{t('tasks.lane')}</dt><dd>{detail.lane}</dd></div>
              <div className="task-detail-wide"><dt>{t('common.server')}</dt><dd><Link className="text-link" to={serverRoute(instanceId)} state={returnToState(location.pathname, location.search)}>{instance.profile.displayName}</Link></dd></div>
              <div className="task-detail-wide"><dt>{t('common.owner')}</dt><dd>{detail.owner?.agentId ? <Link className="text-link" to={agentRoute(instanceId, detail.owner.agentId)} state={returnToState(location.pathname, location.search)}>{detail.owner.agentName}</Link> : detail.owner?.agentName ?? '—'}</dd></div>
              {detail.candidateRef ? <div className="task-detail-wide"><dt>{t('common.candidate')}</dt><dd><code>{detail.candidateRef}</code></dd></div> : null}
            </dl>
            {(detail.participants ?? []).length > 0 ? (
              <div className="task-participants">
                <span className="muted">{t('tasks.participants')}</span>
                <div className="chip-row">
                  {(detail.participants ?? []).map((participant) => participant.agentId ? <Link className="text-link" key={participant.agentId} to={agentRoute(instanceId, participant.agentId)} state={returnToState(location.pathname, location.search)}>{participant.agentName}</Link> : <span className="chip" key={`${participant.agentName}:${participant.claimedAt}`}>{participant.agentName}</span>)}
                </div>
              </div>
            ) : null}
            {detail.tags.length > 0 || detail.details || detail.result !== undefined || JSON.stringify(detail.checkpoint) !== '{}' ? (
              <details className="task-technical-details">
                <summary>{t('tasks.technicalDetails')}</summary>
                {detail.tags.length > 0 ? <div className="chip-row">{detail.tags.map((tag) => <span className="chip" key={tag}>{tag}</span>)}</div> : null}
                {detail.checkpoint ? <pre>{JSON.stringify(detail.checkpoint, null, 2)}</pre> : null}
                {detail.details ? <pre>{JSON.stringify(detail.details, null, 2)}</pre> : null}
                {detail.result !== undefined ? <pre>{JSON.stringify(detail.result, null, 2)}</pre> : null}
              </details>
            ) : null}
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
          <div className="task-filters" aria-label={t('tasks.filters')}>
            <label className="server-switcher ui-field"><span>{t('tasks.namespace')}</span><select aria-label={t('tasks.namespace')} value={namespaceFilter} onChange={(event) => setNamespaceFilter(event.target.value)}><option value="all">{t('tasks.allNamespaces')}</option>{namespaces.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
            <label className="server-switcher ui-field"><span>{t('tasks.state')}</span><select aria-label={t('tasks.state')} value={stateFilter} onChange={(event) => setStateFilter(event.target.value as StateFilter)}><option value="open">{t('tasks.open')}</option><option value="ready">{t('tasks.ready')}</option><option value="in_progress">{t('tasks.inProgress')}</option><option value="blocked">{t('tasks.blocked')}</option><option value="deferred">{t('tasks.deferred')}</option><option value="done">{t('tasks.completed')}</option><option value="all">{t('tasks.all')}</option></select></label>
            <label className="server-switcher ui-field"><span>{t('tasks.operationalStatus')}</span><select aria-label={t('tasks.operationalStatus')} value={operationalFilter} onChange={(event) => setOperationalFilter(event.target.value as OperationalFilter)}><option value="all">{t('tasks.all')}</option><option value="ready">{t('tasks.ready')}</option><option value="in_progress">{t('tasks.inProgress')}</option><option value="blocked">{t('tasks.blocked')}</option><option value="deferred">{t('tasks.deferred')}</option><option value="done">{t('tasks.completed')}</option></select></label>
          </div>
          <div className="task-list" aria-label={t('tasks.serverTasks')}>
            {filteredTasks.map((task) => (
              <Link className="task-row" key={task.key} to={taskRoute(instanceId, task.namespace, task.taskId)} aria-label={`${task.taskId} · ${task.title}`}>
                <div className="task-row-main">
                  <strong className="task-row-title">{task.title}</strong>
                  <code className="task-row-id">{task.taskId}</code>
                  <span className="task-row-meta">{task.namespace} · {task.priority} · {task.lane}</span>
                  {task.nextAction ? <span className="task-row-next">{task.nextAction}</span> : null}
                </div>
                <span className="chip task-row-status">{taskStatusLabel(task.operationalStatus, t)}</span>
                <span className="task-row-chevron" aria-hidden="true"><Icon name="chevron-right" /></span>
              </Link>
            ))}
            {filteredTasks.length === 0 && <FeedbackState variant="empty" title={t('tasks.noMatchingTasks')} />}
          </div>
        </>
      )}

    </section>
  )
}
