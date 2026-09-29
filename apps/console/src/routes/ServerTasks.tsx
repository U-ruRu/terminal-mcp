import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import type { TaskReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'

export type ServerTaskLoader = (
  instanceId: string,
  namespace: string,
  taskId: string,
) => Promise<TaskReadModel>

function taskHref(instanceId: string, task: TaskReadModel): string {
  return '/servers/' + encodeURIComponent(instanceId) + '/tasks/' +
    encodeURIComponent(task.namespace) + '/' + encodeURIComponent(task.taskId)
}

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
  const cached = namespace && taskId
    ? tasks.find((task) => task.namespace === namespace && task.taskId === taskId)
    : undefined
  const requestedKey = namespace && taskId ? namespace + '/' + taskId : ''
  const [loaded, setLoaded] = useState<{ key: string; task?: TaskReadModel; error?: string }>({ key: '' })
  const detail = loaded.key === requestedKey && loaded.task ? loaded.task : cached
  const error = loaded.key === requestedKey ? (loaded.error ?? '') : ''

  useEffect(() => {
    if (!instance || !namespace || !taskId || !loadTask) return
    if (instance.runtime.authStatus !== 'connected') return
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

      {detail && (
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
            {detail.owner && <span className="chip">{t('common.owner')} {detail.owner.agentName}</span>}
            {detail.candidateRef && <span className="chip">{t('common.candidate')} {detail.candidateRef.slice(0, 12)}</span>}
          </div>
          {error && <p className="muted">{t('tasks.refreshFailed')} {error}. {t('tasks.cachedRemains')}</p>}
        </article>
      )}

      <div className="task-list" aria-label={t('tasks.serverTasks')}>
        {tasks.map((task) => (
          <article className="panel" key={task.key}>
            <div className="section-heading">
              <div>
                <strong>{task.taskId} · {task.title}</strong>
                <p className="muted">{task.namespace} · {task.priority} · {task.lane}</p>
              </div>
              <span className="chip">{task.operationalStatus}</span>
            </div>
            <p>{task.nextAction || t('tasks.noNextAction')}</p>
            <Link className="text-link" to={taskHref(instanceId, task)}>{t('tasks.openDetail')}</Link>
          </article>
        ))}
        {tasks.length === 0 && (
          <article className="panel"><p className="muted">{t('tasks.noTasks')}</p></article>
        )}
      </div>

      <div className="server-actions">
        <Link className="nav-link" to={'/servers/' + encodeURIComponent(instanceId)}>{t('nav.backToServer')}</Link>
        <Link className="nav-link" to={'/activity?server=' + encodeURIComponent(instanceId)}>{t('nav.activity')}</Link>
      </div>
    </section>
  )
}
