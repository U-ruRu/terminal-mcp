import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import type { ActivityFeedReadModel } from '../api/models'
import type { FleetActivityOptions, FleetInstanceView } from '../fleet/types'
import { filterActivityEvents, mergeActivityEvents, type ActivityCategory } from '../activity/timeline'

export type ActivityLoader = (instanceId: string, options?: FleetActivityOptions) => Promise<ActivityFeedReadModel>

const categories: Array<{ value: ActivityCategory; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'messages', label: 'Messages' },
  { value: 'tasks', label: 'Tasks' },
  { value: 'intents', label: 'Intents' },
  { value: 'sessions', label: 'Sessions' },
  { value: 'health', label: 'Health' },
  { value: 'commands', label: 'Commands' },
]

export function Activity({ instances = [], loadActivity }: { instances?: FleetInstanceView[]; loadActivity?: ActivityLoader }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [category, setCategory] = useState<ActivityCategory>('all')
  const [feeds, setFeeds] = useState<Record<string, { events: ActivityFeedReadModel['events']; cursor: number; highWater: number; gap: boolean; error?: string }>>({})

  const requested = searchParams.get('server') ?? ''
  const selectedId = instances.some((item) => item.profile.instanceId === requested)
    ? requested
    : (instances[0]?.profile.instanceId ?? '')
  const selected = instances.find((item) => item.profile.instanceId === selectedId)
  const feed = feeds[selectedId] ?? { events: [], cursor: 0, highWater: 0, gap: false }
  const realtimeCursor = selected?.runtime.realtime?.cursor ?? 0

  useEffect(() => {
    if (!selectedId || !loadActivity || (feed.cursor > 0 && realtimeCursor <= feed.cursor)) return
    let cancelled = false
    void loadActivity(selectedId, { since: feed.cursor, limit: 100 }).then((page) => {
      if (cancelled) return
      setFeeds((current) => {
        const prior = current[selectedId] ?? { events: [], cursor: 0, highWater: 0, gap: false }
        return { ...current, [selectedId]: { events: mergeActivityEvents(prior.events, page.events), cursor: page.nextCursor, highWater: page.highWaterSeq, gap: page.gap } }
      })
    }).catch((reason: unknown) => {
      if (cancelled) return
      setFeeds((current) => {
        const prior = current[selectedId] ?? { events: [], cursor: 0, highWater: 0, gap: false }
        return { ...current, [selectedId]: { ...prior, error: reason instanceof Error ? reason.message : 'activity_load_failed' } }
      })
    })
    return () => { cancelled = true }
  }, [selectedId, loadActivity, realtimeCursor, feed.cursor])

  const visible = useMemo(() => filterActivityEvents(feed.events, category), [feed.events, category])

  const chooseServer = (instanceId: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('server', instanceId)
    setSearchParams(next, { replace: true })
  }

  return (
    <section className="stack" aria-labelledby="activity-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Fleet activity</p>
          <h2 id="activity-title">Activity</h2>
          <p className="muted">Live and replayed operational events, scoped to one Terminal MCP server.</p>
        </div>
        <span className="count-badge">{feed.cursor}/{feed.highWater}</span>
      </div>

      <div className="filter-bar">
        <label>Server
          <select aria-label="Server" value={selectedId} onChange={(event) => chooseServer(event.target.value)}>
            {instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}
          </select>
        </label>
        <label>Category
          <select aria-label="Category" value={category} onChange={(event) => setCategory(event.target.value as ActivityCategory)}>
            {categories.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        {selected && <span className={'status status-' + selected.runtime.status}>{selected.runtime.status}</span>}
      </div>

      {instances.length === 0 && <div className="panel"><p className="muted">No paired servers.</p></div>}
      {feed.gap && <div className="panel"><strong>History gap detected.</strong><p className="muted">The retained journal starts after the requested cursor; refresh from a current snapshot.</p></div>}
      {feed.error && <div className="panel"><strong>Activity unavailable</strong><p className="muted">{feed.error}</p></div>}

      <div className="timeline" aria-label="Activity timeline">
        {visible.map((event) => (
          <article className="panel activity-event" key={selectedId + ':' + event.seq}>
            <div className="section-heading">
              <div><strong>{event.message?.senderName ?? event.actorName ?? event.eventType}</strong><p className="muted">{event.eventType} · #{event.seq}</p></div>
              <span className="chip">{event.entityType}</span>
            </div>
            {event.message ? <p>{event.message.text}</p> : <pre>{JSON.stringify(event.payload, null, 2)}</pre>}
            <div className="chip-row">
              {event.message?.taskNamespace && event.message.taskId && (
                <Link
                  className="text-link"
                  to={
                    '/servers/' + encodeURIComponent(selectedId) + '/tasks/' +
                    encodeURIComponent(event.message.taskNamespace) + '/' +
                    encodeURIComponent(event.message.taskId)
                  }
                >
                  Task {event.message.taskId}
                </Link>
              )}
              {event.actorName && (
                <Link className="text-link" to={'/servers/' + encodeURIComponent(selectedId) + '?agent=' + encodeURIComponent(event.actorName)}>
                  Agent {event.actorName}
                </Link>
              )}
              <span className="muted">{event.createdAt}</span>
            </div>
          </article>
        ))}
        {instances.length > 0 && visible.length === 0 && !feed.error && <div className="panel"><p className="muted">No activity matches this filter.</p></div>}
      </div>
    </section>
  )
}
