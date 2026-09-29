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
      realtime: { status: 'live', snapshot: null, cursor, highWaterSeq: cursor, socketConnected: true, reconnectAttempt: 0 },
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
        { seq: 1, eventType: 'task.updated', entityType: 'task', entityId: 'M2', actorName: 'Yankee', payload: {}, createdAt: '2026-09-28T11:00:01Z' },
        { seq: 2, eventType: 'message.created', entityType: 'message', entityId: 'msg', actorName: 'Alpha', payload: {}, createdAt: '2026-09-28T11:00:02Z', message: { messageHash: 'msg', senderName: 'Alpha', target: 'Bravo', text: 'Ship it', requireReply: false, alert: false, taskNamespace: 'console', taskId: 'M2-009', recipients: [] } },
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
  await userEvent.selectOptions(screen.getByLabelText('Category'), 'messages')
  expect(screen.getByText('Ship it')).toBeInTheDocument()
  expect(screen.queryByText('task.updated · #1')).not.toBeInTheDocument()
  await userEvent.selectOptions(screen.getByLabelText('Server'), 'beta')
  await waitFor(() => expect(load).toHaveBeenCalledWith('beta', expect.objectContaining({ since: 0 })))
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
  await waitFor(() => expect(load).toHaveBeenCalledWith('alpha', expect.objectContaining({ since: 0 })))
})
