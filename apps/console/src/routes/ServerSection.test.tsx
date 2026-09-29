import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { expect, test } from 'vitest'

import type { FleetReadModel } from '../fleet/readModel'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerSection } from './ServerSection'

const model: FleetReadModel = {
  servers: [{
    instanceId: 'secondary', origin: 'https://secondary.example', displayName: 'Secondary',
    connectivity: 'live', freshness: 'fresh', version: '0.10.1', healthy: true,
    contexts: [
      { id: 1, summary: 'Primary rules', content: 'Do the important thing.', primary: true },
      { id: 2, summary: 'Extra note', content: 'Optional detail.', primary: false },
    ],
    reconnectAttempt: 0, activeAgentCount: 0, activeIntents: [],
    taskCounts: { ready: 0, inProgress: 0, blocked: 0, deferred: 0, done: 0, highPriorityOpen: 0 },
    communication: { unread: 0, replyRequired: 0, alerts: 0 }, blockerCount: 0, snapshotAvailable: true,
  }],
  summary: { totalServers: 1, liveServers: 1, staleServers: 0, offlineServers: 0, activeAgents: 0, blockedTasks: 0, replyRequired: 0, alerts: 0 },
  activeAgents: [], sessions: [], blockers: [], recentActivity: [],
}

test('renders actual snapshot context entries for the selected server', () => {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/secondary/context']}>
        <Routes><Route path="/servers/:instanceId/context" element={<ServerSection model={model} section="context" />} /></Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
  expect(screen.getByText('Primary rules')).toBeInTheDocument()
  expect(screen.getByText('Do the important thing.')).toBeInTheDocument()
  expect(screen.getByText('Primary')).toBeInTheDocument()
  expect(screen.getByText('Extra note')).toBeInTheDocument()
  expect(screen.getByText('Additional')).toBeInTheDocument()
})
