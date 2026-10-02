import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import type { ActivityFeedReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { Activity } from './Activity'

afterEach(() => cleanup())

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

test('switches servers, filters messages and renders direct task navigation', async () => {
  const load = activityLoader()
  render(
    <I18nProvider><MemoryRouter initialEntries={['/activity?server=alpha']}>
      <Activity instances={[instance('alpha', 'Alpha', 2), instance('beta', 'Beta', 7)]} loadActivity={load} />
    </MemoryRouter></I18nProvider>,
  )
  await screen.findByText('Ship it')
  expect(screen.getByRole('link', { name: 'Task M2-009' })).toHaveAttribute('href', '/servers/alpha/tasks/console/M2-009')
  expect(screen.getAllByRole('link', { name: 'Server Alpha' })[0]).toHaveAttribute('href', '/servers/alpha')
  expect(screen.getByRole('link', { name: 'Agent Alpha' })).toHaveAttribute('href', '/servers/alpha/agents/Alpha-1111')
  await userEvent.selectOptions(screen.getByLabelText('Category'), 'messages')
  expect(screen.getByText('Ship it')).toBeInTheDocument()
  expect(screen.queryByText('task.updated · #1')).not.toBeInTheDocument()
  await userEvent.selectOptions(screen.getByLabelText('Server'), 'beta')
  await waitFor(() => expect(load).toHaveBeenCalledWith('beta', expect.objectContaining({ before: 8, limit: 100 })))
  await userEvent.selectOptions(screen.getByLabelText('Category'), 'all')
  expect(await screen.findByText('health.changed · #7')).toBeInTheDocument()
})

test('does not silently choose a server when activity has no server context', async () => {
  const load = activityLoader()
  render(
    <I18nProvider><MemoryRouter initialEntries={['/activity']}>
      <Activity instances={[instance('alpha', 'Alpha', 2), instance('beta', 'Beta', 7)]} loadActivity={load} />
    </MemoryRouter></I18nProvider>,
  )
  expect(screen.getByText('Choose a server to view activity.')).toBeInTheDocument()
  expect(load).not.toHaveBeenCalled()
  await userEvent.selectOptions(screen.getByLabelText('Server'), 'alpha')
  await waitFor(() => expect(load).toHaveBeenCalledWith('alpha', expect.objectContaining({ before: 3, limit: 100 })))
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
  expect(await screen.findByText(/second-session/)).toBeInTheDocument()
  expect(screen.queryByText(/first-session/)).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /Agent SameName-2222/ }))
  expect(await screen.findByText(/first-session/)).toBeInTheDocument()
  expect(screen.getByText(/second-session/)).toBeInTheDocument()
})

test('mobile chat viewport ends above the fixed bottom navigation', async () => {
  const width = window.innerWidth
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
  const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains('activity-chat')) return { top: 300, bottom: 700, left: 0, right: 390, width: 390, height: 400, x: 0, y: 300, toJSON: () => ({}) } as DOMRect
    if (this.classList.contains('mobile-bottom-navigation')) return { top: 700, bottom: 760, left: 0, right: 390, width: 390, height: 60, x: 0, y: 700, toJSON: () => ({}) } as DOMRect
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
  })
  const load = activityLoader()

  render(
    <I18nProvider><MemoryRouter initialEntries={['/activity?server=alpha']}>
      <Activity instances={[instance('alpha', 'Alpha', 2)]} loadActivity={load} />
      <nav className="mobile-bottom-navigation" />
    </MemoryRouter></I18nProvider>,
  )

  await screen.findByText('Ship it')
  expect(document.querySelector('.activity-chat')).toHaveStyle({ maxHeight: '392px' })
  rect.mockRestore()
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
})

test('loads a concise latest window and keeps raw payload collapsed', async () => {
  const load = activityLoader()
  render(
    <I18nProvider><MemoryRouter initialEntries={['/activity?server=beta']}>
      <Activity instances={[instance('beta', 'Beta', 7)]} loadActivity={load} />
    </MemoryRouter></I18nProvider>,
  )

  await waitFor(() => expect(load).toHaveBeenCalledWith('beta', expect.objectContaining({ before: 8, limit: 100 })))
  const details = screen.getByText('Technical details').closest('details')
  expect(details).not.toHaveAttribute('open')
  expect(details).toHaveTextContent('"ok": true')
})


test('shows loading feedback before the first activity page resolves', () => {
  const load = vi.fn(() => new Promise<ActivityFeedReadModel>(() => {}))
  render(
    <I18nProvider><MemoryRouter initialEntries={['/activity?server=alpha']}>
      <Activity instances={[instance('alpha', 'Alpha', 2)]} loadActivity={load} />
    </MemoryRouter></I18nProvider>,
  )

  expect(screen.getByText('Catching up')).toBeInTheDocument()
  expect(screen.queryByText('No activity matches this filter.')).not.toBeInTheDocument()
})
