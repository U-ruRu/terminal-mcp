import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import type { ActivityFeedReadModel } from '../api/models'
import { agentRoute, serverRoute, taskRoute } from '../navigation/routes'
import { filterActivityEvents, mergeActivityEvents, type ActivityCategory } from '../activity/timeline'
import type { FleetActivityOptions, FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'

export type ActivityLoader = (instanceId: string, options?: FleetActivityOptions) => Promise<ActivityFeedReadModel>

const ACTIVITY_WINDOW = 100
const HISTORY_BATCH = 250
const SCROLL_EDGE = 72

type FeedState = {
  events: ActivityFeedReadModel['events']
  cursor: number
  highWater: number
  oldestSeq?: number
  gap: boolean
  initialized: boolean
  loading: boolean
  historyMode: boolean
  error?: string
}

const emptyFeed = (): FeedState => ({
  events: [], cursor: 0, highWater: 0, gap: false, initialized: false, loading: false, historyMode: false,
})

const categories: Array<{ value: ActivityCategory; labelKey: MessageKey }> = [
  { value: 'all', labelKey: 'common.all' },
  { value: 'messages', labelKey: 'common.messages' },
  { value: 'tasks', labelKey: 'common.tasks' },
  { value: 'intents', labelKey: 'common.intents' },
  { value: 'sessions', labelKey: 'common.sessions' },
  { value: 'health', labelKey: 'common.health' },
  { value: 'commands', labelKey: 'common.commands' },
]

function reason(error: unknown) {
  return error instanceof Error ? error.message : 'activity_load_failed'
}

export function Activity({ instances = [], loadActivity }: { instances?: FleetInstanceView[]; loadActivity?: ActivityLoader }) {
  const { t, number, dateTime } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const [category, setCategory] = useState<ActivityCategory>('all')
  const [feeds, setFeeds] = useState<Record<string, FeedState>>({})
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)
  const pendingHistoryHeight = useRef<number | null>(null)

  const requested = searchParams.get('server') ?? ''
  const requestedAgentId = searchParams.get('agent') ?? ''
  const selectedId = instances.some((item) => item.profile.instanceId === requested) ? requested : ''
  const selected = instances.find((item) => item.profile.instanceId === selectedId)
  const feed = feeds[selectedId] ?? emptyFeed()
  const realtimeCursor = selected?.runtime.realtime?.cursor ?? 0
  const realtimeHighWater = selected?.runtime.realtime?.highWaterSeq ?? realtimeCursor
  const initialBefore = Math.max(1, realtimeHighWater + 1)
  const firstEventSeq = feed.events[0]?.seq
  const lastEventSeq = feed.events.at(-1)?.seq

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node) return

    const fitChatToViewport = () => {
      if (window.innerWidth > 700) {
        node.style.removeProperty('max-height')
        return
      }
      const top = node.getBoundingClientRect().top
      const bottomNavigation = document.querySelector<HTMLElement>('.mobile-bottom-navigation')
      const navigationTop = bottomNavigation?.getBoundingClientRect().top ?? 0
      const viewportHeight = window.visualViewport?.height ?? window.innerHeight
      const visibleBottom = navigationTop > 0 ? Math.min(navigationTop, viewportHeight) : viewportHeight
      const available = Math.max(260, Math.min(760, Math.floor(visibleBottom - top - 8)))
      node.style.maxHeight = `${available}px`
    }

    fitChatToViewport()
    window.addEventListener('resize', fitChatToViewport)
    window.visualViewport?.addEventListener('resize', fitChatToViewport)
    return () => {
      window.removeEventListener('resize', fitChatToViewport)
      window.visualViewport?.removeEventListener('resize', fitChatToViewport)
    }
  }, [selectedId])

  useEffect(() => {
    if (!selectedId || !loadActivity || feed.initialized) return
    let cancelled = false
    void loadActivity(selectedId, { before: initialBefore, limit: ACTIVITY_WINDOW }).then((page) => {
      if (cancelled) return
      stickToBottom.current = true
      setFeeds((current) => ({
        ...current,
        [selectedId]: {
          events: page.events.slice(-ACTIVITY_WINDOW), cursor: page.highWaterSeq, highWater: page.highWaterSeq,
          oldestSeq: page.oldestSeq, gap: page.gap, initialized: true, loading: false, historyMode: false,
        },
      }))
    }).catch((error: unknown) => {
      if (cancelled) return
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? emptyFeed()), initialized: true, loading: false, error: reason(error) } }))
    })
    return () => { cancelled = true }
  }, [selectedId, loadActivity, initialBefore, feed.initialized])

  useEffect(() => {
    if (!selectedId || !loadActivity || !feed.initialized || feed.loading || feed.historyMode || realtimeHighWater <= feed.cursor) return
    let cancelled = false
    void loadActivity(selectedId, { since: feed.cursor, limit: ACTIVITY_WINDOW }).then((page) => {
      if (cancelled) return
      setFeeds((current) => {
        const prior = current[selectedId] ?? emptyFeed()
        const merged = mergeActivityEvents(prior.events, page.events).slice(-ACTIVITY_WINDOW)
        return { ...current, [selectedId]: { ...prior, events: merged, cursor: page.nextCursor, highWater: page.highWaterSeq, oldestSeq: page.oldestSeq, gap: page.gap, error: undefined } }
      })
    }).catch((error: unknown) => {
      if (cancelled) return
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? emptyFeed()), error: reason(error) } }))
    })
    return () => { cancelled = true }
  }, [selectedId, loadActivity, realtimeHighWater, feed.cursor, feed.initialized, feed.loading, feed.historyMode])

  useEffect(() => {
    const node = scrollRef.current
    if (!node || !feed.initialized) return
    if (pendingHistoryHeight.current !== null) {
      node.scrollTop += node.scrollHeight - pendingHistoryHeight.current
      pendingHistoryHeight.current = null
      return
    }
    if (!feed.historyMode && stickToBottom.current) node.scrollTop = node.scrollHeight
  }, [selectedId, feed.initialized, feed.historyMode, firstEventSeq, lastEventSeq])

  const loadOlder = async () => {
    const node = scrollRef.current
    const firstSeq = feed.events[0]?.seq
    if (!node || !loadActivity || !selectedId || feed.loading || firstSeq === undefined) return
    if (feed.oldestSeq !== undefined && firstSeq <= feed.oldestSeq) return
    pendingHistoryHeight.current = node.scrollHeight
    setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: true } }))
    try {
      const page = await loadActivity(selectedId, { before: firstSeq, limit: HISTORY_BATCH })
      setFeeds((current) => {
        const prior = current[selectedId] ?? feed
        const window = mergeActivityEvents(page.events, prior.events).slice(0, ACTIVITY_WINDOW)
        return { ...current, [selectedId]: { ...prior, events: window, cursor: window.at(-1)?.seq ?? prior.cursor, highWater: page.highWaterSeq, oldestSeq: page.oldestSeq, gap: page.gap, loading: false, historyMode: true, error: undefined } }
      })
    } catch (error: unknown) {
      pendingHistoryHeight.current = null
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: false, error: reason(error) } }))
    }
  }

  const restoreLatest = async () => {
    if (!loadActivity || !selectedId || feed.loading || !feed.historyMode) return
    setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: true } }))
    try {
      const page = await loadActivity(selectedId, { before: Math.max(1, realtimeHighWater + 1), limit: ACTIVITY_WINDOW })
      stickToBottom.current = true
      setFeeds((current) => ({ ...current, [selectedId]: { events: page.events.slice(-ACTIVITY_WINDOW), cursor: page.highWaterSeq, highWater: page.highWaterSeq, oldestSeq: page.oldestSeq, gap: page.gap, initialized: true, loading: false, historyMode: false } }))
    } catch (error: unknown) {
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: false, error: reason(error) } }))
    }
  }

  const onScroll = () => {
    const node = scrollRef.current
    if (!node || !feed.initialized) return
    const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight
    stickToBottom.current = distanceFromBottom <= SCROLL_EDGE
    if (node.scrollTop <= SCROLL_EDGE) void loadOlder()
    else if (feed.historyMode && distanceFromBottom <= SCROLL_EDGE) void restoreLatest()
  }

  const visible = useMemo(() => {
    const categorized = filterActivityEvents(feed.events, category)
    if (!requestedAgentId) return categorized
    return categorized.filter((event) => event.actorId === requestedAgentId || event.message?.senderAgentId === requestedAgentId)
  }, [feed.events, category, requestedAgentId])
  const requestedAgent = selected?.runtime.realtime?.snapshot?.agents.find((agent) => agent.agentId === requestedAgentId)

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
        <div><p className="eyebrow">{t('activity.eyebrow')}</p><h2 id="activity-title">{t('nav.activity')}</h2><p className="muted">{t('activity.description')}</p></div>
        <span className="count-badge">{number(feed.events.length)}</span>
      </div>
      <div className="filter-bar">
        <div className="filter-controls">
          <label className="ui-field">{t('common.server')}<select aria-label={t('common.server')} value={selectedId} onChange={(event) => chooseServer(event.target.value)}><option value="">{t('activity.chooseServer')}</option>{instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}</select></label>
          <label className="ui-field">{t('common.category')}<select aria-label={t('common.category')} value={category} onChange={(event) => setCategory(event.target.value as ActivityCategory)}>{categories.map((item) => <option key={item.value} value={item.value}>{t(item.labelKey)}</option>)}</select></label>
        </div>
        <div className="filter-status">
          {selected && <span className={'status status-' + selected.runtime.status}>{runtimeStatus}</span>}
          {selectedId && requestedAgentId && <button type="button" onClick={clearAgent}>{t('common.agent')} {requestedAgent?.name ?? requestedAgentId} ×</button>}
        </div>
      </div>
      {selected ? <p className="muted activity-provenance">{t('activity.serverJournal')} · {runtimeStatus}{feed.events.length ? ' · ' + dateTime(feed.events[feed.events.length - 1].createdAt) : ''}</p> : null}
      {instances.length === 0 && <FeedbackState variant="empty" title={t('activity.noPairedServers')} />}
      {instances.length > 0 && !selectedId && <FeedbackState variant="empty" title={t('activity.chooseToView')} />}
      {feed.gap && <FeedbackState variant="partial" title={t('activity.historyGap')} detail={t('activity.historyGapDescription')} />}
      {feed.error && <FeedbackState variant="error" title={t('activity.unavailable')} detail={feed.error} />}
      {selectedId && !feed.initialized && !feed.error ? <FeedbackState variant="loading" title={t('status.catchingUp')} /> : null}
      <div className="timeline activity-chat" aria-label={t('activity.timeline')} ref={scrollRef} onScroll={onScroll}>
        {visible.map((event) => (
          <article className="panel activity-event" key={selectedId + ':' + event.seq}>
            <div className="section-heading"><div><strong>{event.message?.senderName ?? event.actorName ?? event.eventType}</strong><p className="muted">{event.eventType} · #{number(event.seq)}</p></div><span className="chip">{event.entityType}</span></div>
            {event.message ? (
              <p className="activity-message">{event.message.text}</p>
            ) : (
              <details className="activity-payload">
                <summary>{t('activity.rawDetails')}</summary>
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </details>
            )}
            <div className="chip-row">
              {selected && <Link className="text-link" to={serverRoute(selectedId)}>{t('common.server')} {selected.profile.displayName}</Link>}
              {event.message?.taskNamespace && event.message.taskId && <Link className="text-link" to={taskRoute(selectedId, event.message.taskNamespace, event.message.taskId)}>{t('common.task')} {event.message.taskId}</Link>}
              {event.actorName && event.actorId ? <Link className="text-link" to={agentRoute(selectedId, event.actorId)}>{t('common.agent')} {event.actorName}</Link> : event.actorName ? <span className="muted">{t('common.agent')} {event.actorName}</span> : null}
              <span className="muted">{dateTime(event.createdAt)}</span>
            </div>
          </article>
        ))}
        {selectedId && feed.initialized && visible.length === 0 && !feed.error && <FeedbackState variant="empty" title={t('activity.noMatches')} />}
      </div>
    </section>
  )
}
