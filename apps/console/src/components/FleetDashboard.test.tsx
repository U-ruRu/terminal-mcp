import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

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

  expect(screen.queryByRole('heading', { name: 'Fleet overview' })).not.toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server A server' })).toHaveAttribute('data-state', 'offline')
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveAttribute('data-state', 'stale')
  expect(screen.getByRole('article', { name: 'Server C server' })).toHaveAttribute('data-state', 'healthy')
  const offlineCard = screen.getByRole('article', { name: 'Server A server' })
  expect(within(offlineCard).queryByRole('progressbar')).not.toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveTextContent('46%')
  const healthyCard = screen.getByRole('article', { name: 'Server C server' })
  expect(healthyCard).toHaveTextContent('28%')
  expect(within(healthyCard).getAllByRole('progressbar')).toHaveLength(3)
  expect(screen.queryByLabelText('Fleet totals')).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Server C · Live' })).toHaveAttribute('href', '/servers/server-c')
})


test('fleet status action filters problem servers and focuses the compact list', async () => {
  render(dashboard())

  const status = screen.getByRole('button', { name: 'Partial / Offline: 2 servers' })
  expect(status).toHaveTextContent('2 Problems')
  await userEvent.click(status)

  const list = screen.getByRole('generic', { name: 'Servers' })
  expect(list).toHaveFocus()
  expect(screen.getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server C server' })).not.toBeInTheDocument()
})

test('filters problem servers without refetching fleet state', async () => {
  render(dashboard())

  await userEvent.click(screen.getByRole('button', { name: 'Problems (2)' }))
  expect(screen.getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server C server' })).not.toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'Live (1)' }))
  expect(screen.getByRole('article', { name: 'Server C server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server B server' })).not.toBeInTheDocument()
})

test('updates incrementally when the supplied fleet model changes', () => {
  const { rerender } = render(dashboard())
  const staleCard = screen.getByRole('article', { name: 'Server B server' })
  expect(staleCard).toHaveAttribute('data-state', 'stale')
  expect(staleCard.querySelector('.server-card-problem')).toBeInTheDocument()

  const updated = {
    ...fixtureFleetModel,
    summary: { ...fixtureFleetModel.summary, liveServers: 2, staleServers: 0, alerts: 0, replyRequired: 0, blockedTasks: 1 },
    servers: fixtureFleetModel.servers.map((server) =>
      server.instanceId === 'server-b'
        ? {
            ...server,
            connectivity: 'live' as const,
            connectionState: 'live' as const,
            freshness: 'fresh' as const,
            healthState: 'healthy' as const,
            communication: { unread: 0, replyRequired: 0, alerts: 0 },
            blockerCount: 0,
            staleReason: undefined,
          }
        : server,
    ),
  }

  rerender(dashboard(updated))
  const recoveredCard = screen.getByRole('article', { name: 'Server B server' })
  expect(recoveredCard).toHaveAttribute('data-state', 'healthy')
  expect(recoveredCard.querySelector('.server-card-problem')).toBeInTheDocument()
  expect(screen.queryByLabelText('Fleet totals')).not.toBeInTheDocument()
})


test('omits activity and legacy shared-session blocks from Fleet dashboard', () => {
  render(dashboard())
  expect(screen.queryByText('Agent continuity')).not.toBeInTheDocument()
  expect(screen.queryByText('Recent activity')).not.toBeInTheDocument()
})


test('uses state transition time and keeps human-readable connection, stale, health and resource reasons together', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-28T11:10:00Z'))
  const model = {
    ...fixtureFleetModel,
    servers: fixtureFleetModel.servers.map((server) => server.instanceId === 'server-b' ? {
      ...server,
      connectivity: 'stale' as const, freshness: 'stale' as const, healthy: false,
      stateSince: '2026-09-28T11:09:30Z', lastSeenAt: '2026-09-28T09:00:00Z',
      staleReason: 'cursor_gap', lastError: 'network_error',
      resources: { ...server.resources!, cpu: { status: 'available' as const, logicalCores: 4, usagePercent: 95, load1m: 3.8, load5m: 3, load15m: 2 } },
    } : server),
  }
  render(dashboard(model))
  const card = screen.getByRole('article', { name: 'Server B server' })
  expect(card).toHaveTextContent('Event stream gap detected')
  expect(card).toHaveTextContent('Network connection failed')
  expect(card).toHaveTextContent('Server health check reports a problem')
  expect(card).toHaveTextContent('Resource usage requires attention')
  expect(card).toHaveTextContent('95%')
  expect(card).toHaveTextContent('30s')
  expect(card).not.toHaveTextContent('cursor_gap')
  expect(card).not.toHaveTextContent('network_error')
  vi.useRealTimers()
})
