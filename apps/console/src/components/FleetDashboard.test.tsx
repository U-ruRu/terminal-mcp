import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import { fixtureFleetModel } from '../fixtures/fleet'
import { I18nProvider } from '../i18n/I18nProvider'
import { FleetDashboard } from './FleetDashboard'

afterEach(() => cleanup())

function dashboard(model = fixtureFleetModel) {
  return (
    <I18nProvider><MemoryRouter>
      <FleetDashboard model={model} />
    </MemoryRouter></I18nProvider>
  )
}

test('renders mixed fleet health and makes unavailable telemetry explicit', () => {
  render(dashboard())

  expect(screen.getByRole('heading', { name: 'Fleet overview' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server A server' })).toHaveAttribute('data-state', 'offline')
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveAttribute('data-state', 'stale')
  expect(screen.getByRole('article', { name: 'Server C server' })).toHaveAttribute('data-state', 'healthy')
  const offlineCard = screen.getByRole('article', { name: 'Server A server' })
  expect(within(offlineCard).getAllByLabelText('Unavailable')).toHaveLength(3)
  expect(within(offlineCard).queryByRole('progressbar')).not.toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveTextContent('46%')
  expect(screen.getByRole('article', { name: 'Server C server' })).toHaveTextContent('28%')
  expect(within(screen.getByRole('article', { name: 'Server C server' })).getAllByRole('progressbar')).toHaveLength(3)
  expect(screen.queryByLabelText('Fleet totals')).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Server C · Live' })).toHaveAttribute('href', '/servers/server-c')
})


test('fleet status action filters problem servers and focuses the compact list', async () => {
  render(dashboard())

  await userEvent.click(screen.getByRole('button', { name: 'Partial / Offline: 2 servers' }))

  const list = screen.getByRole('generic', { name: 'Servers' })
  expect(list).toHaveFocus()
  expect(screen.getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server C server' })).not.toBeInTheDocument()
})

test('filters problem servers without refetching fleet state', async () => {
  render(dashboard())

  await userEvent.click(screen.getByRole('button', { name: 'Needs attention (2)' }))
  expect(screen.getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server C server' })).not.toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'Live (1)' }))
  expect(screen.getByRole('article', { name: 'Server C server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server B server' })).not.toBeInTheDocument()
})

test('updates incrementally when the supplied fleet model changes', () => {
  const { rerender } = render(dashboard())
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveAttribute('data-state', 'stale')

  const updated = {
    ...fixtureFleetModel,
    summary: { ...fixtureFleetModel.summary, liveServers: 2, staleServers: 0, alerts: 0, replyRequired: 0, blockedTasks: 1 },
    servers: fixtureFleetModel.servers.map((server) =>
      server.instanceId === 'server-b'
        ? {
            ...server,
            connectivity: 'live' as const,
            freshness: 'fresh' as const,
            communication: { unread: 0, replyRequired: 0, alerts: 0 },
            blockerCount: 0,
            staleReason: undefined,
          }
        : server,
    ),
  }

  rerender(dashboard(updated))
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveAttribute('data-state', 'healthy')
  expect(screen.queryByLabelText('Fleet totals')).not.toBeInTheDocument()
})


test('renders shared-session continuity without exposing private identity', () => {
  const model = {
    ...fixtureFleetModel,
    summary: { ...fixtureFleetModel.summary, activeAgents: 1 },
    sessions: [
      {
        sessionRef: 'session-safe-ref',
        name: 'Alpha',
        originInstanceId: 'server-a',
        sessionAgeSeconds: 1200,
        sessionRemainingSeconds: 180,
        scopedIntents: [],
        attachments: [
          {
            instanceId: 'server-a',
            origin: 'https://server-a.example',
            displayName: 'Server A',
            status: 'active',
            intent: 'origin task',
            currentStep: 3,
            lastActivityAt: '2026-09-28T10:00:00Z',
          },
          {
            instanceId: 'server-b',
            origin: 'https://server-b.example',
            displayName: 'Server B',
            status: 'active',
            intent: 'continued task',
            currentStep: 4,
            lastActivityAt: '2026-09-28T10:01:00Z',
            attachment: true,
          },
        ],
      },
    ],
  }

  render(dashboard(model))

  const session = screen.getByRole('listitem', { name: 'Alpha global session' })
  expect(session).toHaveTextContent('origin server-a')
  expect(session).toHaveTextContent('2 attached')
  expect(session).toHaveTextContent('remaining 180s')
  expect(session).toHaveTextContent('Server A: origin task')
  expect(session).toHaveTextContent('Server B: continued task')
  expect(session).not.toHaveTextContent('private')
})
