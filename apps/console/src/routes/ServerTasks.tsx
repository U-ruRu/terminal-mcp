import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import type { TaskReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'

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
  const { instanceId = '', namespace, taskId } = useParams()
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
          <h2 id="server-tasks-title">Tasks</h2>
          <p className="muted">Authoritative task snapshot for this Terminal MCP server.</p>
        </div>
        <span className={'status status-' + instance.runtime.status}>{instance.runtime.status}</span>
      </div>

      {instance.runtime.status !== 'live' && (
        <div className="attention-strip" role="status">
          Showing cached tasks while the server is {instance.runtime.status}.
        </div>
      )}

      {detail && (
        <article className="panel" aria-label="Task detail">
          <div className="section-heading">
            <div>
              <p className="eyebrow">{detail.namespace}</p>
              <h3>{detail.taskId} · {detail.title}</h3>
            </div>
            <span className="chip">{detail.operationalStatus}</span>
          </div>
          <p>{detail.nextAction || 'No next action recorded.'}</p>
          <div className="chip-row">
            <span className="chip">{detail.priority}</span>
            <span className="chip">{detail.lane}</span>
            {detail.owner && <span className="chip">Owner {detail.owner.agentName}</span>}
            {detail.candidateRef && <span className="chip">Candidate {detail.candidateRef.slice(0, 12)}</span>}
          </div>
          {error && <p className="muted">Live detail refresh failed: {error}. Cached detail remains visible.</p>}
        </article>
      )}

      <div className="task-list" aria-label="Server tasks">
        {tasks.map((task) => (
          <article className="panel" key={task.key}>
            <div className="section-heading">
              <div>
                <strong>{task.taskId} · {task.title}</strong>
                <p className="muted">{task.namespace} · {task.priority} · {task.lane}</p>
              </div>
              <span className="chip">{task.operationalStatus}</span>
            </div>
            <p>{task.nextAction || 'No next action recorded.'}</p>
            <Link className="text-link" to={taskHref(instanceId, task)}>Open detail</Link>
          </article>
        ))}
        {tasks.length === 0 && (
          <article className="panel"><p className="muted">No tasks in the cached snapshot.</p></article>
        )}
      </div>

      <div className="server-actions">
        <Link className="nav-link" to={'/servers/' + encodeURIComponent(instanceId)}>Back to server</Link>
        <Link className="nav-link" to={'/activity?server=' + encodeURIComponent(instanceId)}>Activity</Link>
      </div>
    </section>
  )
}
