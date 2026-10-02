import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import type { ActivityFeedReadModel } from '../api/models'
import { agentRoute, taskRoute } from '../navigation/routes'
import { filterActivityEvents, mergeActivityEvents, type ActivityCategory } from '../activity/timeline'
import { activityFullTimestamp, activityTime, actorHue, projectActivity, renderActivity } from '../activity/chatProjection'
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

function userError(code: string, t: ReturnType<typeof useI18n>['t']): string {
  if (code.includes('auth_unpaired')) return t('diagnostics.serverUnpaired')
  if (code.includes('direct_authority_auth_revoked') || code.includes('auth_revoked')) return t('diagnostics.authorizationRevoked')
  return t('diagnostics.connectionError')
}

export function Activity({ instances = [], loadActivity }: { instances?: FleetInstanceView[]; loadActivity?: ActivityLoader }) {
  const { t, locale } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const [category, setCategory] = useState<ActivityCategory>('all')
  const [feeds, setFeeds] = useState<Record<string, FeedState>>({})
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(() => new Set())
  const [newItemsCount, setNewItemsCount] = useState(0)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)
  const pendingHistoryHeight = useRef<number | null>(null)
  const awayFromBottom = useRef(false)

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
        node.style.removeProperty('height')
        node.style.removeProperty('max-height')
        return
      }
      const top = node.getBoundingClientRect().top
      const bottomNavigation = document.querySelector<HTMLElement>('.mobile-bottom-navigation')
      const navigationTop = bottomNavigation?.getBoundingClientRect().top ?? 0
      const visualViewport = window.visualViewport
      const viewportBottom = visualViewport
        ? visualViewport.offsetTop + visualViewport.height
        : window.innerHeight
      const visibleBottom = navigationTop > 0 ? Math.min(navigationTop, viewportBottom) : viewportBottom
      const available = Math.max(120, Math.floor(visibleBottom - top))
      node.style.height = `${available}px`
      node.style.maxHeight = `${available}px`
    }

    fitChatToViewport()
    const frame = window.requestAnimationFrame(fitChatToViewport)
    const bottomNavigation = document.querySelector<HTMLElement>('.mobile-bottom-navigation')
    const filterBar = node.parentElement?.querySelector<HTMLElement>('.filter-bar')
    const resizeObserver = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(fitChatToViewport)
    if (bottomNavigation) resizeObserver?.observe(bottomNavigation)
    if (filterBar) resizeObserver?.observe(filterBar)
    window.addEventListener('resize', fitChatToViewport)
    window.addEventListener('orientationchange', fitChatToViewport)
    window.visualViewport?.addEventListener('resize', fitChatToViewport)
    window.visualViewport?.addEventListener('scroll', fitChatToViewport)
    return () => {
      window.cancelAnimationFrame(frame)
      resizeObserver?.disconnect()
      window.removeEventListener('resize', fitChatToViewport)
      window.removeEventListener('orientationchange', fitChatToViewport)
      window.visualViewport?.removeEventListener('resize', fitChatToViewport)
      window.visualViewport?.removeEventListener('scroll', fitChatToViewport)
    }
  }, [selectedId, feed.initialized, feed.error, feed.gap, requestedAgentId])

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
      if (awayFromBottom.current && page.events.length > 0) {
        setNewItemsCount((current) => current + projectActivity(page.events, locale, selected?.profile.displayName ?? selectedId).length)
      }
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
  }, [selectedId, loadActivity, realtimeHighWater, feed.cursor, feed.initialized, feed.loading, feed.historyMode, locale, selected?.profile.displayName])

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
    awayFromBottom.current = distanceFromBottom > node.clientHeight
    if (stickToBottom.current) setNewItemsCount(0)
    if (node.scrollTop <= SCROLL_EDGE) void loadOlder()
    else if (feed.historyMode && distanceFromBottom <= SCROLL_EDGE) void restoreLatest()
  }

  const visible = useMemo(() => {
    const categorized = filterActivityEvents(feed.events, category)
    if (!requestedAgentId) return categorized
    return categorized.filter((event) => event.actorId === requestedAgentId || event.message?.senderAgentId === requestedAgentId)
  }, [feed.events, category, requestedAgentId])
  const requestedAgent = selected?.runtime.realtime?.snapshot?.agents.find((agent) => agent.agentId === requestedAgentId)
  const logicalItems = useMemo(
    () => projectActivity(visible, locale, selected?.profile.displayName ?? selectedId),
    [visible, locale, selected?.profile.displayName, selectedId],
  )
  const chatItems = useMemo(() => renderActivity(logicalItems, locale), [logicalItems, locale])

  useEffect(() => {
    setExpandedEvents(new Set())
    setNewItemsCount(0)
    stickToBottom.current = true
    awayFromBottom.current = false
    const frame = window.requestAnimationFrame(() => {
      const node = scrollRef.current
      if (node) node.scrollTop = node.scrollHeight
    })
    return () => window.cancelAnimationFrame(frame)
  }, [selectedId, category, requestedAgentId])

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
  return (
    <section className="activity-screen" aria-label={t('nav.activity')}>
      <div className="filter-bar">
        <div className="filter-controls">
          <label className="ui-field">{t('common.server')}<select aria-label={t('common.server')} value={selectedId} onChange={(event) => chooseServer(event.target.value)}><option value="">{t('activity.chooseServer')}</option>{instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}</select></label>
          <label className="ui-field">{t('common.category')}<select aria-label={t('common.category')} value={category} onChange={(event) => setCategory(event.target.value as ActivityCategory)}>{categories.map((item) => <option key={item.value} value={item.value}>{t(item.labelKey)}</option>)}</select></label>
        </div>
        {selectedId && requestedAgentId ? (
          <div className="filter-status">
            <button type="button" onClick={clearAgent}>{t('common.agent')} {requestedAgent?.name ?? requestedAgentId} ×</button>
          </div>
        ) : null}
      </div>
      {instances.length === 0 && <FeedbackState variant="empty" title={t('activity.noPairedServers')} />}
      {instances.length > 0 && !selectedId && <FeedbackState variant="empty" title={t('activity.chooseToView')} />}
      {feed.gap && <FeedbackState variant="partial" title={t('activity.historyGap')} detail={t('activity.historyGapDescription')} />}
      {feed.error ? (
        <div className="activity-error-state">
          <FeedbackState variant="error" title={t('activity.unavailable')} detail={userError(feed.error, t)} />
          <details className="inline-technical-details">
            <summary aria-label={t('activity.rawDetails')}><span aria-hidden="true">⌄</span></summary>
            <code>{feed.error}</code>
          </details>
        </div>
      ) : null}
      {selectedId && !feed.initialized && !feed.error ? <FeedbackState variant="loading" title={t('status.catchingUp')} /> : null}
      {selectedId && !feed.error ? (
        <div className="activity-timeline-wrap">
          <div className="timeline activity-chat" aria-label={t('activity.timeline')} ref={scrollRef} onScroll={onScroll}>
            {chatItems.map((item) => {
              if (item.kind === 'date') return <div className="activity-date-separator" key={item.key}>{item.label}</div>
              const eventKey = selectedId + ':' + item.key
              const expanded = expandedEvents.has(eventKey)
              const detailsId = 'activity-details-' + eventKey.replace(/[^a-zA-Z0-9_-]/g, '-')
              const first = item.events[0]
              if (item.kind === 'service') {
                return (
                  <div className={'activity-service-event activity-service-' + (item.tone ?? 'neutral')} key={item.key}>
                    <span>{item.content}</span>
                    <time dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>
                    <button type="button" className="activity-details-toggle" aria-label={t('activity.rawDetails')} aria-expanded={expanded} aria-controls={detailsId}
                      onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(eventKey)) next.delete(eventKey); else next.add(eventKey); return next })}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d={expanded ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'} /></svg>
                    </button>
                    {expanded ? <pre id={detailsId} className="activity-technical-payload">{JSON.stringify({
                      timestamp: activityFullTimestamp(item.createdAt), eventType: first.eventType, entityType: first.entityType,
                      entityId: first.entityId, events: item.events,
                    }, null, 2)}</pre> : null}
                  </div>
                )
              }
              const actorName = item.actorName ?? 'Unknown agent'
              return (
                <article className={'activity-chat-message' + (item.showIdentity ? ' activity-group-start' : ' activity-group-continuation')} key={item.key}>
                  <div className="activity-identity-column">
                    {item.showIdentity ? <span className="activity-identity-marker" style={{ '--activity-actor-hue': actorHue(actorName) } as React.CSSProperties} aria-hidden="true">{actorName.charAt(0).toUpperCase()}</span> : null}
                  </div>
                  <div className={'activity-chat-content' + (item.commands?.length ? ' activity-command-bubble' : '') + (item.commands?.length && expanded ? ' is-expanded' : '')}>
                    {item.showIdentity ? <div className="activity-chat-header">
                      {item.actorId ? <Link className="activity-actor-name" to={agentRoute(selectedId, item.actorId)}>{actorName}</Link> : <strong className="activity-actor-name">{actorName}</strong>}
                      <time dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>
                    </div> : <time className="activity-continuation-time" dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>}
                    <div className="activity-message-line">
                      <p>{item.content}</p>
                      {item.secondary ? <small>{item.secondary}</small> : null}
                      {item.taskNamespace && item.taskId ? <Link className="activity-context-link text-link" to={taskRoute(selectedId, item.taskNamespace, item.taskId)}>{t('common.task')} {item.taskId}</Link> : null}
                    </div>
                    <button type="button" className="activity-details-toggle" aria-label={item.commands?.length ? item.content : t('activity.rawDetails')} aria-expanded={expanded} aria-controls={detailsId}
                      onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(eventKey)) next.delete(eventKey); else next.add(eventKey); return next })}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d={expanded ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'} /></svg>
                    </button>
                    {expanded && item.commands?.length ? (
                      <div id={detailsId} className="activity-command-list">
                        {item.commands.map((command, commandIndex) => {
                          const commandKey = eventKey + ':' + command.key
                          const commandExpanded = expandedEvents.has(commandKey)
                          const commandDetailsId = 'activity-command-details-' + commandKey.replace(/[^a-zA-Z0-9_-]/g, '-')
                          return (
                            <div className="activity-command-row" key={command.key}>
                              <div className="activity-command-summary">
                                <span>{command.label ?? (item.commands!.length > 1 ? t('common.commands') + ' ' + (commandIndex + 1) : t('common.commands'))}</span>
                                <time dateTime={command.createdAt}>{activityTime(command.createdAt)}</time>
                                <small>{command.status}</small>
                              </div>
                              <button type="button" className="activity-command-detail-toggle" aria-label={t('activity.rawDetails')} aria-expanded={commandExpanded} aria-controls={commandDetailsId}
                                onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(commandKey)) next.delete(commandKey); else next.add(commandKey); return next })}>
                                <svg viewBox="0 0 24 24" aria-hidden="true"><path d={commandExpanded ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'} /></svg>
                              </button>
                              {commandExpanded ? <pre id={commandDetailsId} className="activity-technical-payload">{JSON.stringify({
                                timestamp: activityFullTimestamp(command.createdAt),
                                events: command.events,
                              }, null, 2)}</pre> : null}
                            </div>
                          )
                        })}
                      </div>
                    ) : expanded ? <pre id={detailsId} className="activity-technical-payload">{JSON.stringify({
                      timestamp: activityFullTimestamp(item.createdAt), eventType: first.eventType, entityType: first.entityType,
                      entityId: first.entityId, actorId: first.actorId, actorName: first.actorName, events: item.events,
                    }, null, 2)}</pre> : null}
                  </div>
                </article>
              )
            })}
          {feed.initialized && visible.length === 0 && <div className="activity-chat-empty">{t('activity.noMatches')}</div>}
          </div>
          {newItemsCount > 0 ? (
            <button className="activity-new-items" type="button" onClick={() => {
              const node = scrollRef.current
              if (node) node.scrollTop = node.scrollHeight
              stickToBottom.current = true
              awayFromBottom.current = false
              setNewItemsCount(0)
            }}>↓ {newItemsCount}</button>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
