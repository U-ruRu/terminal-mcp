import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import { fixtureFleetModel } from '../fixtures/fleet'
import { FleetDashboard } from './FleetDashboard'

afterEach(() => cleanup())

function dashboard(model = fixtureFleetModel) {
  return (
    <MemoryRouter>
      <FleetDashboard model={model} />
    </MemoryRouter>
  )
}

test('renders mixed fleet health and makes unavailable telemetry explicit', () => {
  render(dashboard())

  expect(screen.getByRole('heading', { name: 'Fleet overview' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server A server' })).toHaveTextContent('offline')
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveTextContent('stale')
  expect(screen.getByRole('article', { name: 'Server C server' })).toHaveTextContent('fresh')
  expect(screen.getByRole('article', { name: 'Server A server' })).toHaveTextContent('Unavailable')
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveTextContent('cursor_gap')
  expect(screen.getByRole('article', { name: 'Server C server' })).toHaveTextContent('28%')
})

test('filters problem servers without refetching fleet state', async () => {
  render(dashboard())

  await userEvent.click(screen.getByRole('button', { name: 'Needs attention' }))
  expect(screen.getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server C server' })).not.toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'Live' }))
  expect(screen.getByRole('article', { name: 'Server C server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server B server' })).not.toBeInTheDocument()
})

test('updates incrementally when the supplied fleet model changes', () => {
  const { rerender } = render(dashboard())
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveTextContent('stale')

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
  expect(screen.getByRole('article', { name: 'Server B server' })).toHaveTextContent('fresh')
  expect(screen.getByLabelText('Fleet totals')).toHaveTextContent('Live2')
})
