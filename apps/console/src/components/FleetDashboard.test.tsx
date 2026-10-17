import { cleanup,render,screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test } from 'vitest'

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

test('fleet status action filters problem servers and focuses the compact list', async () => {
  render(dashboard())

  const status = screen.getByRole('button', { name: 'Partial / Offline: 2 servers' })
  await userEvent.click(status)

  const list = screen.getByRole('generic', { name: 'Servers' })
  expect(list).toHaveFocus()
  expect(screen.getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(screen.getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(screen.queryByRole('article', { name: 'Server C server' })).not.toBeInTheDocument()
})

test('updates incrementally when the supplied fleet model changes', () => {
  const { rerender } = render(dashboard())

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
})
