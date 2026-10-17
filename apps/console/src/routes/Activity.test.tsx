import { cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import { activityStateCache,resetActivityStateCacheForTests } from '../activity/stateCache'
import type { ActivityFeedReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { Activity } from './Activity'

afterEach(() => { cleanup(); resetActivityStateCacheForTests() })

function instance(instanceId: string, displayName: string, cursor: number): FleetInstanceView {
  return {
    profile: {
      instanceId,
      origin: `https://${instanceId}.example`,
      displayName,
      credentialRef: `cred-${instanceId}`,
      metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 },
      createdAt: 1,
      updatedAt: 1,
    },
    runtime: {
      instanceId,
      status: 'live',
      authStatus: 'connected',
      reconnectAttempt: 0,
      realtime: { status: 'live', snapshot: null, cursor, highWaterSeq: cursor, socketConnected: true, reconnectAttempt: 0, freshness: 'fresh', catchingUpScopes: [] },
    },
  }
}

function page(since: number, events: ActivityFeedReadModel['events'], highWaterSeq: number): ActivityFeedReadModel {
  return { events, since, nextCursor: events.at(-1)?.seq ?? since, highWaterSeq, gap: false }
}

function activityLoader() {
  return vi.fn(async (instanceId: string, options = {}) => {
    const since = (options as { since?: number }).since ?? 0
    if (instanceId === 'alpha') {
      return page(since, [
        { seq: 1, eventType: 'task.updated', entityType: 'task', entityId: 'M2', actorId: 'Yankee-1111', actorName: 'Yankee', payload: {}, createdAt: '2026-09-28T11:00:01Z' },
        { seq: 2, eventType: 'message.created', entityType: 'message', entityId: 'msg', actorId: 'Alpha-1111', actorName: 'Alpha', payload: {}, createdAt: '2026-09-28T11:00:02Z', message: { messageHash: 'msg', senderAgentId: 'Alpha-1111', senderName: 'Alpha', target: 'Bravo', text: 'Ship it', requireReply: false, alert: false, taskNamespace: 'console', taskId: 'M2-009', recipients: [] } },
      ], 2)
    }
    return page(since, [
      { seq: 7, eventType: 'health.changed', entityType: 'health', entityId: 'terminal-mcp', payload: { ok: true }, createdAt: '2026-09-28T11:00:07Z' },
    ], 7)
  })
}

test('selecting another server loads that server Activity', async () => {
  const load = activityLoader()
  render(<I18nProvider><MemoryRouter initialEntries={['/activity?server=alpha']}>
    <Activity instances={[instance('alpha', 'Alpha', 2), instance('beta', 'Beta', 7)]} loadActivity={load} />
  </MemoryRouter></I18nProvider>)
  await screen.findByText('Ship it')

  await userEvent.selectOptions(screen.getByLabelText('Server'), 'beta')

  expect(await screen.findByText('Beta is online again')).toBeInTheDocument()
  expect(load).toHaveBeenCalledWith('beta', { before: Number.MAX_SAFE_INTEGER, limit: 100 })
})

test('loads the latest Tokyo Activity even before realtime snapshot high-water is ready', async () => {
  const load = vi.fn(async (_instanceId: string, options = {}) => {
    const before = (options as { before?: number }).before
    if (before === Number.MAX_SAFE_INTEGER) {
      return {
        ...page(0, [{
          seq: 79725, eventType: 'message.created', entityType: 'message', entityId: 'tokyo-message',
          actorId: 'TokyoAgent-1111', actorName: 'TokyoAgent', payload: {}, createdAt: '2026-10-05T19:29:11Z',
          message: { messageHash: 'tokyo-message', senderAgentId: 'TokyoAgent-1111', senderName: 'TokyoAgent', target: 'operator', text: 'Tokyo activity visible', requireReply: false, alert: false, recipients: [] },
        }], 79725),
        oldestSeq: 29726,
      }
    }
    return page(0, [], 79725)
  })

  render(
    <I18nProvider><MemoryRouter initialEntries={['/activity?server=tokyo']}>
      <Activity instances={[instance('tokyo', 'Tokyo', 0)]} loadActivity={load} />
    </MemoryRouter></I18nProvider>,
  )

  await waitFor(() => expect(load).toHaveBeenCalledWith('tokyo', { before: Number.MAX_SAFE_INTEGER, limit: 100 }))
  await screen.findByText('Tokyo activity visible')
})

function messageEvent(seq: number, text: string): ActivityFeedReadModel['events'][number] {
  return { seq, eventType: 'message.created', entityType: 'message', entityId: `msg-${seq}`,
    actorId: 'Alpha-1111', actorName: 'Alpha', payload: {}, createdAt: '2026-09-28T11:00:00Z',
    message: { messageHash: `msg-${seq}`, senderAgentId: 'Alpha-1111', senderName: 'Alpha',
      target: 'Bravo', text, requireReply: false, alert: false, recipients: [] } }
}
function seedHistory() {
  activityStateCache.set('alpha', {
    feed: { events: [messageEvent(101, 'Message 101'), messageEvent(102, 'Message 102')],
      cursor: 102, highWater: 102, oldestSeq: 1, gap: false, initialized: true, loading: false, historyMode: true },
    category: 'messages', agentId: '', newItemsCount: 0, stickToBottom: false,
  })
}

test('reconnecting preserves displayed Activity history and its viewport', () => {
  seedHistory()
  const live = instance('alpha', 'Alpha', 102)
  const tree = (server: FleetInstanceView) => <I18nProvider><MemoryRouter initialEntries={['/activity?server=alpha']}>
    <Activity instances={[server]} />
  </MemoryRouter></I18nProvider>
  const { rerender } = render(tree(live))
  const chat = document.querySelector('.activity-chat') as HTMLDivElement
  chat.scrollTop = 137
  const reconnecting: FleetInstanceView = { ...live, runtime: { ...live.runtime,
    status: 'reconnecting', reconnectAttempt: 1,
    realtime: { ...live.runtime.realtime!, status: 'reconnecting', socketConnected: false, freshness: 'stale' },
  } }

  rerender(tree(reconnecting))

  expect(screen.getByLabelText('Category')).toHaveValue('messages')
  expect(screen.getByText('Message 101')).toBeInTheDocument()
  expect(chat.scrollTop).toBe(137)
})

test('scrolling to the oldest Activity item requests the preceding page', async () => {
  seedHistory()
  const load = vi.fn(async () => ({ ...page(0, [messageEvent(51, 'Older 51')], 102), oldestSeq: 1 }))
  render(<I18nProvider><MemoryRouter initialEntries={['/activity?server=alpha']}>
    <Activity instances={[instance('alpha', 'Alpha', 102)]} loadActivity={load} />
  </MemoryRouter></I18nProvider>)
  const chat = document.querySelector('.activity-chat') as HTMLDivElement
  chat.scrollTop = 0

  fireEvent.scroll(chat)

  expect(await screen.findByText('Older 51')).toBeInTheDocument()
  expect(load).toHaveBeenCalledWith('alpha', { before: 101, limit: 250 })
})

test('filters duplicate public names by exact agent session identity', async () => {
  const load = vi.fn(async (_instanceId: string, options = {}) => {
    const since = (options as { since?: number }).since ?? 0
    return page(since, [
      { seq: 11, eventType: 'agent.activity', entityType: 'agent', entityId: 'SameName-1111', actorId: 'SameName-1111', actorName: 'SameName', payload: { marker: 'first-session' }, createdAt: '2026-09-28T11:01:01Z' },
      { seq: 12, eventType: 'agent.activity', entityType: 'agent', entityId: 'SameName-2222', actorId: 'SameName-2222', actorName: 'SameName', payload: { marker: 'second-session' }, createdAt: '2026-09-28T11:01:02Z' },
    ], 12)
  })
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/activity?server=alpha&agent=SameName-2222']}>
        <Activity instances={[instance('alpha', 'Alpha', 12)]} loadActivity={load} />
      </MemoryRouter>
    </I18nProvider>,
  )
  await waitFor(() => expect(document.querySelectorAll('.activity-chat-message')).toHaveLength(1))
  expect(screen.getByText('SameName')).toBeInTheDocument()
  expect(screen.getByText(/Agent SameName-2222/)).toBeInTheDocument()
})

test('Technical details reveals the selected Activity event payload', async () => {
  render(<I18nProvider><MemoryRouter initialEntries={['/activity?server=beta']}>
    <Activity instances={[instance('beta', 'Beta', 7)]} loadActivity={activityLoader()} />
  </MemoryRouter></I18nProvider>)
  const toggle = await screen.findByRole('button', { name: 'Technical details' })

  await userEvent.click(toggle)

  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText(/"ok": true/)).toBeInTheDocument()
})
