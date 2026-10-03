import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { Icon } from '../components/Icon'

import type { ActivityFeedReadModel } from '../api/models'
import { returnToState } from '../navigation/context'
import { agentRoute, taskRoute } from '../navigation/routes'
import { filterActivityEvents, mergeActivityEvents, type ActivityCategory } from '../activity/timeline'
import { activityStateCache, type ActivityFeedState } from '../activity/stateCache'
import { activityFullTimestamp, activityTime, actorHue, projectActivity, renderActivity } from '../activity/chatProjection'
import { captureActivityScrollAnchor, restoreActivityScrollAnchor, type ActivityScrollAnchor } from '../activity/scrollAnchor'
import type { FleetActivityOptions, FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'

export type ActivityLoader = (instanceId: string, options?: FleetActivityOptions) => Promise<ActivityFeedReadModel>

const ACTIVITY_WINDOW = 100
const HISTORY_BATCH = 250
const BOTTOM_EDGE = 24
const TOP_EDGE = 96
const SCROLL_IDLE_MS = 140

const emptyFeed = (): ActivityFeedState => ({
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
  if (typeof node.scrollTo === 'function') node.scrollTo({ top: node.scrollHeight, behavior })
  else node.scrollTop = node.scrollHeight
}

function activityServerLabel(instance: FleetInstanceView): string {
  const configured = instance.profile.displayName.trim()
  const haystack = (instance.profile.instanceId + ' ' + configured).toLowerCase()
  const aliases: Array<[string, string]> = [
    ['tokyo', 'Tokyo'],
    ['bacloud', 'BacLOUD'],
    ['backcloud', 'BacLOUD'],
    ['firstbyte', 'Firstbyte'],
    ['secondary', 'Secondary'],
    ['main', 'Main'],
  ]
  if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(configured)) {
    const match = aliases.find(([needle]) => haystack.includes(needle))
    if (match) return match[1]
  }
  return configured || instance.profile.instanceId
}


function commandStateLabel(status: string, t: ReturnType<typeof useI18n>['t']): string {
  if (/complete|success|done|finished/.test(status)) return t('activity.commandCompleted')
  if (/fail|error/.test(status)) return t('activity.commandFailed')
  if (/cancel/.test(status)) return t('activity.commandCancelled')
  if (/running|active|execut/.test(status)) return t('activity.commandRunning')
  if (/queued|pending/.test(status)) return t('activity.commandQueued')
  return t('common.unknown')
}

function userError(code: string, t: ReturnType<typeof useI18n>['t']): string {
  if (code.includes('auth_unpaired')) return t('diagnostics.serverUnpaired')
  if (code.includes('direct_authority_auth_revoked') || code.includes('auth_revoked')) return t('diagnostics.authorizationRevoked')
  return t('diagnostics.connectionError')
}

export function Activity({ instances = [], loadActivity }: { instances?: FleetInstanceView[]; loadActivity?: ActivityLoader }) {
  const { t, locale } = useI18n()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedAtMount = searchParams.get('server') ?? ''
  const cachedAtMount = activityStateCache.get(requestedAtMount)
  const [category, setCategory] = useState<ActivityCategory>(() => cachedAtMount?.category ?? 'all')
  const [feeds, setFeeds] = useState<Record<string, ActivityFeedState>>(() => Object.fromEntries(
    [...activityStateCache].map(([instanceId, state]) => [instanceId, state.feed]),
  ))
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(() => new Set())
  const [newItemsCount, setNewItemsCount] = useState(() => cachedAtMount?.newItemsCount ?? 0)
  const [enteringItems, setEnteringItems] = useState<Set<string>>(() => new Set())
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(cachedAtMount?.stickToBottom ?? true)
  const pendingHistoryAnchor = useRef<ActivityScrollAnchor | null>(cachedAtMount?.anchor ?? null)
  const historyRequestPending = useRef(false)
  const pendingOlderPage = useRef<ActivityFeedReadModel | null>(null)
  const scrollState = useRef<'idle' | 'dragging' | 'flinging'>('idle')
  const scrollIdleTimer = useRef<number | undefined>(undefined)
  const awayFromBottom = useRef(false)

  const requested = searchParams.get('server') ?? ''
  const requestedAgentId = searchParams.get('agent') ?? ''
  const selectedId = instances.some((item) => item.profile.instanceId === requested) ? requested : ''
  const selected = instances.find((item) => item.profile.instanceId === selectedId)
  const selectedServerName = selected ? activityServerLabel(selected) : selectedId
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
  const restoredServerRef = useRef<string>('')

  useLayoutEffect(() => {
    if (!selectedId || restoredServerRef.current === selectedId) return
    restoredServerRef.current = selectedId
    const cached = activityStateCache.get(selectedId)
    if (!cached) {
      setCategory('all')
      setNewItemsCount(0)
      stickToBottom.current = true
      awayFromBottom.current = false
      pendingHistoryAnchor.current = null
      return
    }
    setFeeds((current) => current[selectedId]?.initialized ? current : { ...current, [selectedId]: cached.feed })
    setCategory(cached.category)
    setNewItemsCount(cached.newItemsCount)
    stickToBottom.current = cached.stickToBottom
    awayFromBottom.current = !cached.stickToBottom
    pendingHistoryAnchor.current = cached.anchor ?? null
    if (!requestedAgentId && cached.agentId) {
      const next = new URLSearchParams(searchParams)
      next.set('agent', cached.agentId)
      setSearchParams(next, { replace: true })
    }
  }, [requestedAgentId, searchParams, selectedId, setSearchParams])

  useLayoutEffect(() => {
    if (!selectedId) return
    const node = scrollRef.current
    return () => {
      activityStateCache.set(selectedId, {
        feed,
        category,
        agentId: requestedAgentId,
        anchor: node && feed.initialized ? captureActivityScrollAnchor(node) : undefined,
        newItemsCount,
        stickToBottom: stickToBottom.current,
      })
    }
  }, [category, feed, newItemsCount, requestedAgentId, selectedId])

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
    if (!selectedId || !loadActivity || !feed.initialized || feed.loading || realtimeHighWater <= feed.cursor) return
    let cancelled = false
    void loadActivity(selectedId, { since: feed.cursor, limit: ACTIVITY_WINDOW }).then((page) => {
      if (cancelled) return
      const serverName = selectedServerName
      const forCurrentView = (events: ActivityFeedReadModel['events']) => {
        const categorized = filterActivityEvents(events, category)
        return requestedAgentId
          ? categorized.filter((event) => event.actorId === requestedAgentId || event.message?.senderAgentId === requestedAgentId || event.payload.logical_agent_id === requestedAgentId)
          : categorized
      }
      const beforeItems = projectActivity(forCurrentView(feed.events), locale, serverName, agentNames)
      const beforeKeys = new Set(beforeItems.map((item) => item.key))
      const mergedEvents = mergeActivityEvents(feed.events, page.events)
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
  }, [selectedId, loadActivity, realtimeHighWater, feed.cursor, feed.events, feed.initialized, feed.loading, locale, selectedServerName, agentNames, category, requestedAgentId])

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || !feed.initialized) return
    const anchor = pendingHistoryAnchor.current
    if (anchor) {
      restoreActivityScrollAnchor(node, anchor)
      pendingHistoryAnchor.current = null
      return
    }
    if (stickToBottom.current && scrollState.current === 'idle') {
      const behavior: ScrollBehavior = enteringItems.size > 0 ? 'smooth' : 'auto'
      scrollActivityToBottom(node, behavior)
    }
  }, [selectedId, feed.initialized, feed.historyMode, firstEventSeq, lastEventSeq, enteringItems])

  const captureVisibleAnchor = () => {
    const node = scrollRef.current
    if (!node) return
    pendingHistoryAnchor.current = captureActivityScrollAnchor(node)
  }

  const commitOlderPage = (page: ActivityFeedReadModel) => {
    captureVisibleAnchor()
    setFeeds((current) => {
      const prior = current[selectedId] ?? feed
      const merged = mergeActivityEvents(page.events, prior.events)
      return {
        ...current,
        [selectedId]: {
          ...prior,
          events: merged,
          cursor: Math.max(prior.cursor, merged.at(-1)?.seq ?? 0),
          highWater: Math.max(prior.highWater, page.highWaterSeq),
          oldestSeq: page.oldestSeq,
          gap: prior.gap || page.gap,
          loading: false,
          historyMode: true,
          error: undefined,
        },
      }
    })
    pendingOlderPage.current = null
    historyRequestPending.current = false
  }

  const flushPendingOlder = () => {
    if (scrollState.current !== 'idle' || !pendingOlderPage.current) return
    commitOlderPage(pendingOlderPage.current)
  }

  const scheduleScrollIdle = () => {
    if (scrollIdleTimer.current !== undefined) window.clearTimeout(scrollIdleTimer.current)
    scrollIdleTimer.current = window.setTimeout(() => {
      scrollState.current = 'idle'
      const node = scrollRef.current
      if (node) {
        const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight
        stickToBottom.current = distanceFromBottom <= BOTTOM_EDGE
        awayFromBottom.current = distanceFromBottom > BOTTOM_EDGE
        if (stickToBottom.current) setNewItemsCount(0)
      }
      flushPendingOlder()
    }, SCROLL_IDLE_MS)
  }

  const loadOlder = async () => {
    const firstSeq = feed.events[0]?.seq
    if (!loadActivity || !selectedId || historyRequestPending.current || feed.loading || firstSeq === undefined) return
    if (feed.oldestSeq !== undefined && firstSeq <= feed.oldestSeq) return
    historyRequestPending.current = true
    setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: true } }))
    try {
      const page = await loadActivity(selectedId, { before: firstSeq, limit: HISTORY_BATCH })
      if (scrollState.current === 'idle') commitOlderPage(page)
      else {
        pendingOlderPage.current = page
        setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: false } }))
      }
    } catch (error: unknown) {
      pendingHistoryAnchor.current = null
      pendingOlderPage.current = null
      historyRequestPending.current = false
      setFeeds((current) => ({ ...current, [selectedId]: { ...(current[selectedId] ?? feed), loading: false, error: reason(error) } }))
    }
  }

  const onScroll = () => {
    const node = scrollRef.current
    if (!node || !feed.initialized) return
    if (scrollState.current !== 'dragging') scrollState.current = 'flinging'
    scheduleScrollIdle()
    stickToBottom.current = false
    awayFromBottom.current = true
    if (node.scrollTop <= TOP_EDGE) void loadOlder()
  }

  const onTouchStart = () => {
    scrollState.current = 'dragging'
    stickToBottom.current = false
    awayFromBottom.current = true
    if (scrollIdleTimer.current !== undefined) window.clearTimeout(scrollIdleTimer.current)
  }

  const onTouchEnd = () => {
    scrollState.current = 'flinging'
    scheduleScrollIdle()
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
    () => projectActivity(visible, locale, selectedServerName, agentNames),
    [visible, locale, selectedServerName, agentNames],
  )
  const chatItems = useMemo(() => renderActivity(logicalItems, locale), [logicalItems, locale])

  const chooseServer = (instanceId: string) => {
    const next = new URLSearchParams(searchParams)
    if (instanceId) next.set('server', instanceId)
    else next.delete('server')
    const cached = instanceId ? activityStateCache.get(instanceId) : undefined
    if (cached?.agentId) next.set('agent', cached.agentId)
    else next.delete('agent')
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
          <label className="ui-field">{t('common.server')}<select aria-label={t('common.server')} value={selectedId} onChange={(event) => chooseServer(event.target.value)}><option value="">{t('activity.chooseServer')}</option>{instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{activityServerLabel(item)}</option>)}</select></label>
          <label className="ui-field">{t('common.category')}<select aria-label={t('common.category')} value={category} onChange={(event) => setCategory(event.target.value as ActivityCategory)}>{categories.map((item) => <option key={item.value} value={item.value}>{t(item.labelKey)}</option>)}</select></label>
        </div>
        {selectedId && requestedAgentId ? (
          <div className="filter-status">
            <button type="button" onClick={clearAgent}>{t('common.agent')} {requestedAgent?.name ?? requestedAgentId} <Icon name="close" /></button>
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
              <Icon name={errorExpanded ? 'chevron-up' : 'chevron-down'} />
            </button>
            {errorExpanded ? <code id={errorDetailsId}>{feed.error}</code> : null}
          </div>
        )
      })() : null}
      {selectedId && !feed.initialized && !feed.error ? <FeedbackState variant="loading" title={t('status.catchingUp')} /> : null}
      {selectedId && !feed.error ? (
        <div className="activity-timeline-wrap">
          <div className="timeline activity-chat" aria-label={t('activity.timeline')} ref={scrollRef} onScroll={onScroll} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd} onTouchCancel={onTouchEnd}>
            {feed.loading && feed.initialized ? <div className="activity-history-loading" aria-label={t('status.catchingUp')}><Icon name="loading" /></div> : null}
            {chatItems.map((item) => {
              if (item.kind === 'date') return <div className="activity-date-separator" data-activity-key={item.key} key={item.key}>{item.label}</div>
              const eventKey = selectedId + ':' + item.key
              const expanded = expandedEvents.has(eventKey)
              const detailsId = 'activity-details-' + eventKey.replace(/[^a-zA-Z0-9_-]/g, '-')
              const first = item.events[0]
              if (item.kind === 'service') {
                return (
                  <div className={'activity-service-event activity-service-' + (item.tone ?? 'neutral') + (enteringItems.has(item.key) ? ' activity-item-entering' : '')} data-activity-key={item.key} data-activity-source-seqs={item.events.map((event) => event.seq).join(' ')} key={item.key}>
                    <span>{item.content}</span>
                    <time dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>
                    <button type="button" className="activity-details-toggle" aria-label={t('activity.rawDetails')} aria-expanded={expanded} aria-controls={detailsId}
                      onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(eventKey)) next.delete(eventKey); else next.add(eventKey); return next })}>
                      <Icon name={expanded ? 'chevron-up' : 'chevron-down'} />
                    </button>
                    {expanded ? <pre id={detailsId} className="activity-technical-payload">{JSON.stringify({
                      timestamp: activityFullTimestamp(item.createdAt), server: selectedServerName,
                      eventType: first.eventType, entityType: first.entityType, entityId: first.entityId, events: item.events,
                    }, null, 2)}</pre> : null}
                  </div>
                )
              }
              const actorName = item.actorName ?? 'Unknown agent'
              return (
                <article className={'activity-chat-message' + (item.showIdentity ? ' activity-group-start' : ' activity-group-continuation') + (enteringItems.has(item.key) ? ' activity-item-entering' : '')} data-activity-key={item.key} data-activity-source-seqs={item.events.map((event) => event.seq).join(' ')} key={item.key}>
                  <div className="activity-identity-column">
                    {item.showIdentity ? <span className="activity-identity-marker" style={{ '--activity-actor-hue': actorHue(item.actorId ?? actorName) } as React.CSSProperties} aria-hidden="true">{item.actorAnonymous ? '•' : actorName.charAt(0).toUpperCase()}</span> : null}
                  </div>
                  <div className={'activity-chat-content' + (item.commands?.length ? ' activity-command-bubble' : '') + (item.commands?.length && expanded ? ' is-expanded' : '')}>
                    {item.showIdentity ? <div className="activity-chat-header">
                      {item.actorId ? <Link className="activity-actor-name" to={agentRoute(selectedId, item.actorId)} state={returnToState(location.pathname, location.search)}>{actorName}</Link> : <strong className="activity-actor-name">{actorName}</strong>}
                      <time dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>
                    </div> : <time className="activity-continuation-time" dateTime={item.createdAt}>{activityTime(item.createdAt)}</time>}
                    <div className="activity-message-line">
                      <p>{item.content}</p>
                      {item.secondary ? <small>{item.secondary}</small> : null}
                      {item.taskNamespace && item.taskId ? <Link className="activity-context-link text-link" to={taskRoute(selectedId, item.taskNamespace, item.taskId)} state={returnToState(location.pathname, location.search)}>{t('common.task')} {item.taskId}</Link> : null}
                    </div>
                    <button type="button" className="activity-details-toggle" aria-label={item.commands?.length ? item.content : t('activity.rawDetails')} aria-expanded={expanded} aria-controls={detailsId}
                      onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(eventKey)) next.delete(eventKey); else next.add(eventKey); return next })}>
                      <Icon name={expanded ? 'chevron-up' : 'chevron-down'} />
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
                                <small>{commandStateLabel(command.statusKey, t)}</small>
                              </div>
                              <button type="button" className="activity-command-detail-toggle" aria-label={t('activity.rawDetails')} aria-expanded={commandExpanded} aria-controls={commandDetailsId}
                                onClick={() => setExpandedEvents((current) => { const next = new Set(current); if (next.has(commandKey)) next.delete(commandKey); else next.add(commandKey); return next })}>
                                <Icon name={commandExpanded ? 'chevron-up' : 'chevron-down'} />
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
                      timestamp: activityFullTimestamp(item.createdAt), server: selectedServerName,
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
            }}><Icon name="chevron-down" /> {newItemsCount}</button>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
