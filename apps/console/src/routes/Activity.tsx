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
const HISTORY_RETAIN = 1000
const BOTTOM_EDGE = 24
const TOP_EDGE = 64

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


function scrollActivityToBottom(node: HTMLDivElement, behavior: ScrollBehavior = 'auto') {
  if (typeof node.scrollTo === 'function') scrollActivityToBottom(node, behavior)
  else node.scrollTop = node.scrollHeight
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
  const [enteringItems, setEnteringItems] = useState<Set<string>>(() => new Set())
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)
  const pendingHistoryAnchor = useRef<{ key: string; offset: number } | null>(null)
  const historyRequestPending = useRef(false)
  const awayFromBottom = useRef(false)

  const requested = searchParams.get('server') ?? ''
  const requestedAgentId = searchParams.get('agent') ?? ''
  const selectedId = instances.some((item) => item.profile.instanceId === requested) ? requested : ''
  const selected = instances.find((item) => item.profile.instanceId === selectedId)
  const feed = feeds[selectedId] ?? emptyFeed()
  const realtimeCursor = selected?.runtime.realtime?.cursor ?? 0
  const realtimeHighWater = selected?.runtime.realtime?.highWaterSeq ?? realtimeCursor
  const agentNames = useMemo(
    () => Object.fromEntries((selected?.runtime.realtime?.snapshot?.agents ?? []).map((agent) => [agent.agentId, agent.name])),
    [selected?.runtime.realtime?.snapshot?.agents],
  )
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
      const serverName = selected?.profile.displayName ?? selectedId
      const forCurrentView = (events: ActivityFeedReadModel['events']) => {
        const categorized = filterActivityEvents(events, category)
        return requestedAgentId
          ? categorized.filter((event) => event.actorId === requestedAgentId || event.message?.senderAgentId === requestedAgentId || event.payload.logical_agent_id === requestedAgentId)
          : categorized
      }
      const beforeItems = projectActivity(forCurrentView(feed.events), locale, serverName, agentNames)
      const beforeKeys = new Set(beforeItems.map((item) => item.key))
      const mergedEvents = mergeActivityEvents(feed.events, page.events).slice(-ACTIVITY_WINDOW)
      const afterItems = projectActivity(forCurrentView(mergedEvents), locale, serverName, agentNames)
      const newKeys = afterItems.map((item) => item.key).filter((key) => !beforeKeys.has(key))
      if (newKeys.length > 0) {
        if (awayFromBottom.current) setNewItemsCount((current) => current + newKeys.length)
        else {
          setEnteringItems(new Set(newKeys))
          window.setTimeout(() => setEnteringItems(new Set()), 220)
        }
      }
      setFeeds((current) => {
        const prior = current[selectedId] ?? emptyFeed()
        return { ...current, [selectedId]: { ...prior, events: mergedEvents, cursor: page.nextCursor, highWater: page.highWaterSeq, oldestSeq: page.oldestSeq, gap: page.gap, error: undefined } }
      })
    }).catch((error: unknown) => {
      if (cancelled) return
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? emptyFeed()), error: reason(error) } }))
    })
    return () => { cancelled = true }
  }, [selectedId, loadActivity, realtimeHighWater, feed.cursor, feed.events, feed.initialized, feed.loading, feed.historyMode, locale, selected?.profile.displayName, agentNames, category, requestedAgentId])

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || !feed.initialized) return
    const anchor = pendingHistoryAnchor.current
    if (anchor) {
      const target = [...node.querySelectorAll<HTMLElement>('[data-activity-key]')]
        .find((element) => element.dataset.activityKey === anchor.key)
      if (target) {
        const nextOffset = target.getBoundingClientRect().top - node.getBoundingClientRect().top
        node.scrollTop += nextOffset - anchor.offset
      }
      pendingHistoryAnchor.current = null
      return
    }
    if (!feed.historyMode && stickToBottom.current) {
      const behavior: ScrollBehavior = enteringItems.size > 0 ? 'smooth' : 'auto'
      scrollActivityToBottom(node, behavior)
    }
  }, [selectedId, feed.initialized, feed.historyMode, firstEventSeq, lastEventSeq, enteringItems])

  const loadOlder = async () => {
    const node = scrollRef.current
    const firstSeq = feed.events[0]?.seq
    if (!node || !loadActivity || !selectedId || historyRequestPending.current || feed.loading || firstSeq === undefined) return
    if (feed.oldestSeq !== undefined && firstSeq <= feed.oldestSeq) return
    const nodeTop = node.getBoundingClientRect().top
    const firstVisible = [...node.querySelectorAll<HTMLElement>('[data-activity-key]')]
      .find((element) => element.getBoundingClientRect().bottom > nodeTop)
    if (firstVisible?.dataset.activityKey) {
      pendingHistoryAnchor.current = {
        key: firstVisible.dataset.activityKey,
        offset: firstVisible.getBoundingClientRect().top - nodeTop,
      }
    }
    historyRequestPending.current = true
    setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: true } }))
    try {
      const page = await loadActivity(selectedId, { before: firstSeq, limit: HISTORY_BATCH })
      setFeeds((current) => {
        const prior = current[selectedId] ?? feed
        const window = mergeActivityEvents(page.events, prior.events).slice(-HISTORY_RETAIN)
        return { ...current, [selectedId]: { ...prior, events: window, cursor: window.at(-1)?.seq ?? prior.cursor, highWater: page.highWaterSeq, oldestSeq: page.oldestSeq, gap: page.gap, loading: false, historyMode: true, error: undefined } }
      })
    } catch (error: unknown) {
      pendingHistoryAnchor.current = null
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: false, error: reason(error) } }))
    } finally {
      historyRequestPending.current = false
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
    stickToBottom.current = distanceFromBottom <= BOTTOM_EDGE
    awayFromBottom.current = distanceFromBottom > BOTTOM_EDGE
    if (stickToBottom.current) setNewItemsCount(0)
    if (node.scrollTop <= TOP_EDGE) void loadOlder()
    else if (feed.historyMode && distanceFromBottom <= BOTTOM_EDGE) void restoreLatest()
  }

  const visible = useMemo(() => {
    const categorized = filterActivityEvents(feed.events, category)
    if (!requestedAgentId) return categorized
    return categorized.filter((event) =>
      event.actorId === requestedAgentId
      || event.message?.senderAgentId === requestedAgentId
      || event.payload.logical_agent_id === requestedAgentId
      || event.payload.agent_id === requestedAgentId
    )
  }, [feed.events, category, requestedAgentId])
  const requestedAgent = selected?.runtime.realtime?.snapshot?.agents.find((agent) => agent.agentId === requestedAgentId)
  const logicalItems = useMemo(
    () => projectActivity(visible, locale, selected?.profile.displayName ?? selectedId, agentNames),
    [visible, locale, selected?.profile.displayName, selectedId, agentNames],
  )
  const chatItems = useMemo(() => renderActivity(logicalItems, locale), [logicalItems, locale])

  useLayoutEffect(() => {
    setExpandedEvents(new Set())
    setNewItemsCount(0)
    setEnteringItems(new Set())
    stickToBottom.current = true
    awayFromBottom.current = false
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
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
      {feed.error ? (() => {
        const errorKey = 'activity-error:' + selectedId
        const errorExpanded = expandedEvents.has(errorKey)
        const errorDetailsId = 'activity-error-details-' + selectedId.replace(/[^a-zA-Z0-9_-]/g, '-')
        return (
          <div className="activity-error-service" role="alert">
            <div>
              <strong>{t('activity.unavailable')}</strong>
              <span>{userError(feed.error, t)}</span>
            </div>
            <button type="button" className="activity-error-detail-toggle" aria-label={t('activity.rawDetails')} aria-expanded={errorExpanded} aria-controls={errorDetailsId}
              onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(errorKey)) next.delete(errorKey); else next.add(errorKey); return next })}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d={errorExpanded ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'} /></svg>
            </button>
            {errorExpanded ? <code id={errorDetailsId}>{feed.error}</code> : null}
          </div>
        )
      })() : null}
      {selectedId && !feed.initialized && !feed.error ? <FeedbackState variant="loading" title={t('status.catchingUp')} /> : null}
      {selectedId && !feed.error ? (
        <div className="activity-timeline-wrap">
          <div className="timeline activity-chat" aria-label={t('activity.timeline')} ref={scrollRef} onScroll={onScroll}>
            {feed.loading && feed.initialized ? <div className="activity-history-loading" aria-label={t('status.catchingUp')}>•••</div> : null}
            {chatItems.map((item) => {
              if (item.kind === 'date') return <div className="activity-date-separator" data-activity-key={item.key} key={item.key}>{item.label}</div>
              const eventKey = selectedId + ':' + item.key
              const expanded = expandedEvents.has(eventKey)
              const detailsId = 'activity-details-' + eventKey.replace(/[^a-zA-Z0-9_-]/g, '-')
              const first = item.events[0]
              if (item.kind === 'service') {
                return (
                  <div className={'activity-service-event activity-service-' + (item.tone ?? 'neutral') + (enteringItems.has(item.key) ? ' activity-item-entering' : '')} data-activity-key={item.key} key={item.key}>
                    <span>{item.content}</span>
                    <time dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>
                    <button type="button" className="activity-details-toggle" aria-label={t('activity.rawDetails')} aria-expanded={expanded} aria-controls={detailsId}
                      onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(eventKey)) next.delete(eventKey); else next.add(eventKey); return next })}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d={expanded ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'} /></svg>
                    </button>
                    {expanded ? <pre id={detailsId} className="activity-technical-payload">{JSON.stringify({
                      timestamp: activityFullTimestamp(item.createdAt), server: selected?.profile.displayName ?? selectedId,
                      eventType: first.eventType, entityType: first.entityType, entityId: first.entityId, events: item.events,
                    }, null, 2)}</pre> : null}
                  </div>
                )
              }
              const actorName = item.actorName ?? 'Unknown agent'
              return (
                <article className={'activity-chat-message' + (item.showIdentity ? ' activity-group-start' : ' activity-group-continuation') + (enteringItems.has(item.key) ? ' activity-item-entering' : '')} data-activity-key={item.key} key={item.key}>
                  <div className="activity-identity-column">
                    {item.showIdentity ? <span className="activity-identity-marker" style={{ '--activity-actor-hue': actorHue(item.actorId ?? actorName) } as React.CSSProperties} aria-hidden="true">{item.actorAnonymous ? '•' : actorName.charAt(0).toUpperCase()}</span> : null}
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
                    {expanded && item.commands && item.commands.length > 1 ? (
                      <div id={detailsId} className="activity-command-list">
                        {item.commands.map((command) => {
                          const commandKey = eventKey + ':' + command.key
                          const commandExpanded = expandedEvents.has(commandKey)
                          const commandDetailsId = 'activity-command-details-' + commandKey.replace(/[^a-zA-Z0-9_-]/g, '-')
                          return (
                            <div className="activity-command-row" key={command.key}>
                              <div className="activity-command-summary">
                                <span>{command.label}</span>
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
                      timestamp: activityFullTimestamp(item.createdAt), server: selected?.profile.displayName ?? selectedId,
                      eventType: first.eventType, entityType: first.entityType, entityId: first.entityId,
                      actorIdentity: item.actorId, actorName: item.actorName, events: item.events,
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
              if (node) scrollActivityToBottom(node, 'smooth')
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
