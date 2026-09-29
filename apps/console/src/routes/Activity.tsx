import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import type { ActivityFeedReadModel } from '../api/models'
import { agentRoute, serverRoute, taskRoute } from '../navigation/routes'
import { filterActivityEvents, mergeActivityEvents, type ActivityCategory } from '../activity/timeline'
import type { FleetActivityOptions, FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'

export type ActivityLoader = (instanceId: string, options?: FleetActivityOptions) => Promise<ActivityFeedReadModel>

const categories: Array<{ value: ActivityCategory; labelKey: MessageKey }> = [
  { value: 'all', labelKey: 'common.all' },
  { value: 'messages', labelKey: 'common.messages' },
  { value: 'tasks', labelKey: 'common.tasks' },
  { value: 'intents', labelKey: 'common.intents' },
  { value: 'sessions', labelKey: 'common.sessions' },
  { value: 'health', labelKey: 'common.health' },
  { value: 'commands', labelKey: 'common.commands' },
]

export function Activity({ instances = [], loadActivity }: { instances?: FleetInstanceView[]; loadActivity?: ActivityLoader }) {
  const { t, number, dateTime } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const [category, setCategory] = useState<ActivityCategory>('all')
  const [feeds, setFeeds] = useState<Record<string, { events: ActivityFeedReadModel['events']; cursor: number; highWater: number; gap: boolean; error?: string }>>({})

  const requested = searchParams.get('server') ?? ''
  const requestedAgentId = searchParams.get('agent') ?? ''
  const selectedId = instances.some((item) => item.profile.instanceId === requested) ? requested : ''
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

  const visible = useMemo(() => {
    const categorized = filterActivityEvents(feed.events, category)
    if (!requestedAgentId) return categorized
    return categorized.filter((event) =>
      event.actorId === requestedAgentId || event.message?.senderAgentId === requestedAgentId,
    )
  }, [feed.events, category, requestedAgentId])
  const requestedAgent = selected?.runtime.realtime?.snapshot?.agents.find(
    (agent) => agent.agentId === requestedAgentId,
  )

  const chooseServer = (instanceId: string) => {
    const next = new URLSearchParams(searchParams)
    if (instanceId) next.set('server', instanceId)
    else next.delete('server')
    next.delete('agent')
    setSearchParams(next, { replace: true })
  }

  const clearAgent = () => {
    const next = new URLSearchParams(searchParams)
    next.delete('agent')
    setSearchParams(next, { replace: true })
  }
  const runtimeStatus = selected?.runtime.status === 'live' ? t('status.live')
    : selected?.runtime.status === 'offline' ? t('status.offline')
      : selected?.runtime.status === 'stale' ? t('status.stale')
        : selected?.runtime.status === 'reconnecting' ? t('status.reconnecting')
          : selected?.runtime.status === 'connecting' ? t('status.connecting') : ''

  return (
    <section className="stack" aria-labelledby="activity-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t('activity.eyebrow')}</p>
          <h2 id="activity-title">{t('nav.activity')}</h2>
          <p className="muted">{t('activity.description')}</p>
        </div>
        <span className="count-badge">{number(feed.cursor)}/{number(feed.highWater)}</span>
      </div>

      <div className="filter-bar">
        <div className="filter-controls">
          <label>{t('common.server')}
            <select aria-label={t('common.server')} value={selectedId} onChange={(event) => chooseServer(event.target.value)}>
              <option value="">{t('activity.chooseServer')}</option>
              {instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}
            </select>
          </label>
          <label>{t('common.category')}
            <select aria-label={t('common.category')} value={category} onChange={(event) => setCategory(event.target.value as ActivityCategory)}>
              {categories.map((item) => <option key={item.value} value={item.value}>{t(item.labelKey)}</option>)}
            </select>
          </label>
        </div>
        <div className="filter-status">
          {selected && <span className={'status status-' + selected.runtime.status}>{runtimeStatus}</span>}
          {selectedId && requestedAgentId && (
            <button type="button" onClick={clearAgent}>{t('common.agent')} {requestedAgent?.name ?? requestedAgentId} ×</button>
          )}
        </div>
      </div>
      {selected ? (
        <p className="muted activity-provenance">
          {t('activity.serverJournal')} · {runtimeStatus}
          {feed.events.length ? ' · ' + dateTime(feed.events[feed.events.length - 1].createdAt) : ''}
        </p>
      ) : null}

      {instances.length === 0 && <div className="panel"><p className="muted">{t('activity.noPairedServers')}</p></div>}
      {instances.length > 0 && !selectedId && <div className="panel"><p className="muted">{t('activity.chooseToView')}</p></div>}
      {feed.gap && <div className="panel"><strong>{t('activity.historyGap')}</strong><p className="muted">{t('activity.historyGapDescription')}</p></div>}
      {feed.error && <div className="panel"><strong>{t('activity.unavailable')}</strong><p className="muted">{feed.error}</p></div>}

      <div className="timeline" aria-label={t('activity.timeline')}>
        {visible.map((event) => (
          <article className="panel activity-event" key={selectedId + ':' + event.seq}>
            <div className="section-heading">
              <div><strong>{event.message?.senderName ?? event.actorName ?? event.eventType}</strong><p className="muted">{event.eventType} · #{number(event.seq)}</p></div>
              <span className="chip">{event.entityType}</span>
            </div>
            {event.message ? <p>{event.message.text}</p> : <pre>{JSON.stringify(event.payload, null, 2)}</pre>}
            <div className="chip-row">
              {selected && (
                <Link className="text-link" to={serverRoute(selectedId)}>
                  {t('common.server')} {selected.profile.displayName}
                </Link>
              )}
              {event.message?.taskNamespace && event.message.taskId && (
                <Link className="text-link" to={taskRoute(selectedId, event.message.taskNamespace, event.message.taskId)}>{t('common.task')} {event.message.taskId}</Link>
              )}
              {event.actorName && event.actorId ? (
                <Link className="text-link" to={agentRoute(selectedId, event.actorId)}>{t('common.agent')} {event.actorName}</Link>
              ) : event.actorName ? <span className="muted">{t('common.agent')} {event.actorName}</span> : null}
              <span className="muted">{dateTime(event.createdAt)}</span>
            </div>
          </article>
        ))}
        {selectedId && visible.length === 0 && !feed.error && <div className="panel"><p className="muted">{t('activity.noMatches')}</p></div>}
      </div>
    </section>
  )
}
