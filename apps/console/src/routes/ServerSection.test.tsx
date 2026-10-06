import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import type { KeyValueStorage } from '../auth/vault'
import { BrowserDiagnosticJournal } from '../diagnostics/journal'
import type { FleetReadModel } from '../fleet/readModel'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerSection } from './ServerSection'

afterEach(() => cleanup())

const model: FleetReadModel = {
  servers: [{
    instanceId: 'secondary', origin: 'https://secondary.example', displayName: 'Secondary',
    connectivity: 'live', connectionState: 'live', freshness: 'fresh', healthState: 'healthy', version: '0.10.1', healthy: true,
    contexts: [
      { id: 1, summary: 'Primary rules', content: 'Do the important thing.', primary: true },
      { id: 2, summary: 'Extra note', content: 'Optional detail.', primary: false },
    ],
    reconnectAttempt: 0, catchingUpScopes: [], activeAgentCount: 0, activeIntents: [],
    taskCounts: { ready: 0, inProgress: 0, blocked: 0, deferred: 0, done: 0, highPriorityOpen: 0 },
    communication: { unread: 0, replyRequired: 0, alerts: 0 }, blockerCount: 0, snapshotAvailable: true,
  }],
  summary: { totalServers: 1, liveServers: 1, staleServers: 0, offlineServers: 0, activeAgents: 0, blockedTasks: 0, replyRequired: 0, alerts: 0 },
  activeAgents: [], sessions: [], blockers: [], recentActivity: [],
}

test('context header uses compact orthogonal connectivity/freshness badges without legacy health contradiction', () => {
  const attentionModel: FleetReadModel = {
    ...model,
    servers: [{ ...model.servers[0], healthState: 'attention' }],
  }
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/secondary/context']}>
        <Routes><Route path="/servers/:instanceId/context" element={<ServerSection model={attentionModel} section="context" />} /></Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
  const strip = screen.getByLabelText('Secondary state')
  expect(within(strip).getByText('Live')).toHaveClass('status', 'server-status')
  expect(within(strip).getByText('Fresh')).toHaveClass('status', 'server-status')
  expect(within(strip).queryByText('Needs attention')).not.toBeInTheDocument()
  expect(strip.querySelectorAll('.status')).toHaveLength(2)
})

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


class MemoryStorage implements KeyValueStorage {
  values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

test('health exposes the persistent local diagnostic journal on demand', () => {
  const diagnostics = new BrowserDiagnosticJournal(new MemoryStorage(), () => new Date('2026-09-30T06:00:00Z'))
  diagnostics.append({ type: 'app_background' })
  diagnostics.append({ type: 'connection_state', instanceId: 'secondary', server: 'Secondary', status: 'reconnecting', code: 'dns_error', detail: 'attempt=1' })

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/secondary/health']}>
        <Routes><Route path="/servers/:instanceId/health" element={<ServerSection model={model} section="health" diagnostics={diagnostics} />} /></Routes>
      </MemoryRouter>
    </I18nProvider>,
  )

  expect(screen.getByText(/Local diagnostics/)).toBeInTheDocument()
  expect(screen.getByText(/#1 app_background/)).toBeInTheDocument()
  expect(screen.getByText(/#2 connection_state/)).toBeInTheDocument()
  expect(screen.getByText(/code=dns_error/)).toBeInTheDocument()
  const copy = screen.getByRole('button', { name: 'Copy as text' })
  const clear = screen.getByRole('button', { name: 'Clear log' })
  expect(copy).toBeInTheDocument()
  expect(clear.closest('.diagnostics-actions')).toHaveClass('ui-icon-button-row')
  expect(copy.closest('.diagnostics-actions')).toBe(clear.closest('.diagnostics-actions'))
})


test('clear diagnostics empties the local journal for an isolated test', async () => {
  const diagnostics = new BrowserDiagnosticJournal(new MemoryStorage(), () => new Date('2026-09-30T06:00:00Z'))
  diagnostics.append({ type: 'connection_state', instanceId: 'secondary', server: 'Secondary', status: 'reconnecting', code: 'dns_error' })

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/secondary/health']}>
        <Routes><Route path="/servers/:instanceId/health" element={<ServerSection model={model} section="health" diagnostics={diagnostics} />} /></Routes>
      </MemoryRouter>
    </I18nProvider>,
  )

  expect(screen.getByText(/#1 connection_state/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Clear log' }))
  expect(screen.getByText('No diagnostic events yet.')).toBeInTheDocument()
  expect(diagnostics.list()).toEqual([])
})
