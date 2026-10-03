import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import type { ConsoleSnapshotReadModel, TaskReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerAgents } from './ServerAgents'

const task = (agentId: string, taskId: string): TaskReadModel => ({
  key: `console/${taskId}`,
  namespace: 'console',
  taskId,
  title: `Task ${taskId}`,
  lane: 'implementation',
  priority: 'P1',
  state: 'ready',
  operationalStatus: 'in_progress',
  active: true,
  archived: false,
  tags: [],
  nextAction: 'Continue',
  owner: { agentId, agentName: 'SameName', claimedAt: '2026-09-28T11:00:00Z', claimAgeSeconds: 1, claimIntent: 'Work', role: 'owner' },
  participants: [],
  checkpoint: {},
})

const snapshot: ConsoleSnapshotReadModel = {
  highWaterSeq: 2,
  replayFromSeq: 2,
  duplicateEventsPossible: true,
  instance: {
    application: 'terminal-mcp',
    version: '0.10.1',
    publicBaseUrl: 'https://alpha.example',
    healthy: true,
    health: {},
    resources: {
      status: 'unavailable',
      cpu: { status: 'unavailable' },
      memory: { status: 'unavailable' },
      filesystem: { status: 'unavailable' },
      uptime: { status: 'unavailable' },
    },
  },
  agents: [
    { agentId: 'SameName-1111', name: 'SameName', status: 'active', intent: 'First', currentStep: 1, lastActivity: '1s ago', lastActivityAt: '2026-09-28T11:00:01Z', idleSeconds: 1, sessionAgeSeconds: 60, workScope: [], messagesAwaitingRead: 0, messagesAwaitingReply: 0, alertsPending: 0 },
    { agentId: 'SameName-2222', name: 'SameName', status: 'active', intent: 'Second', currentStep: 2, lastActivity: '2s ago', lastActivityAt: '2026-09-28T11:00:02Z', idleSeconds: 2, sessionAgeSeconds: 120, workScope: [], messagesAwaitingRead: 0, messagesAwaitingReply: 0, alertsPending: 0 },
  ],
  tasks: [task('SameName-1111', 'ONE'), task('SameName-2222', 'TWO')],
  contexts: [],
  communications: [],
}

function instance(status: 'live' | 'offline' = 'live'): FleetInstanceView {
  return {
    profile: { instanceId: 'alpha', origin: 'https://alpha.example', displayName: 'Alpha', credentialRef: 'cred-alpha', metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
    runtime: { instanceId: 'alpha', status, authStatus: 'connected', reconnectAttempt: 0, realtime: { status, snapshot, cursor: 2, highWaterSeq: 2, socketConnected: status === 'live', reconnectAttempt: 0, freshness: status === 'live' ? 'fresh' : 'stale', catchingUpScopes: [] } },
  }
}

function Back() {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(-1)}>Back</button>
}

function renderAgents(path: string, fleet = [instance()]) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/servers/:instanceId/agents" element={<><ServerAgents instances={fleet} /><Back /></>} />
          <Route path="/servers/:instanceId/agents/:agentId" element={<><ServerAgents instances={fleet} /><Back /></>} />
          <Route path="/servers/:instanceId/tasks/:namespace/:taskId" element={<div>Task destination</div>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
}

afterEach(() => cleanup())

test('duplicate public names have distinct stable deep links', () => {
  renderAgents('/servers/alpha/agents')
  const links = screen.getAllByRole('link', { name: 'SameName' })
  expect(links).toHaveLength(2)
  expect(links[0]).toHaveAttribute('href', '/servers/alpha/agents/SameName-1111')
  expect(links[1]).toHaveAttribute('href', '/servers/alpha/agents/SameName-2222')
})

test('direct reload route resolves exact session and only its related task from cached offline state', () => {
  renderAgents('/servers/alpha/agents/SameName-2222', [instance('offline')])
  expect(screen.getByRole('heading', { name: 'SameName' })).toBeInTheDocument()
  expect(screen.getByText('Exact session SameName-2222')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'TWO · Task TWO' })).toHaveAttribute('href', '/servers/alpha/tasks/console/TWO')
  expect(screen.queryByRole('link', { name: 'ONE · Task ONE' })).not.toBeInTheDocument()
})

test('standard links preserve browser history for Back', async () => {
  renderAgents('/servers/alpha/agents')
  const links = screen.getAllByRole('link', { name: 'SameName' })
  await userEvent.click(links[1])
  expect(screen.getByText('Exact session SameName-2222')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Back' }))
  expect(screen.getAllByRole('link', { name: 'SameName' })).toHaveLength(2)
})


test('known agent without an active session keeps a last-known detail surface', () => {
  const staleSnapshot: ConsoleSnapshotReadModel = {
    ...snapshot,
    agents: [{ ...snapshot.agents[0], status: 'suspended', logicalSessionStatus: undefined, intent: 'Last known work' }],
  }
  const stale: FleetInstanceView = {
    ...instance('offline'),
    runtime: { ...instance('offline').runtime, realtime: instance('offline').runtime.realtime ? { ...instance('offline').runtime.realtime!, snapshot: staleSnapshot } : null },
  }
  renderAgents('/servers/alpha/agents/SameName-1111', [stale])
  expect(screen.getByRole('heading', { name: 'SameName' })).toBeInTheDocument()
  expect(screen.getByText('Last known state')).toBeInTheDocument()
  expect(screen.getByText('Last known work')).toBeInTheDocument()
  expect(screen.queryByText('Agent session unavailable')).not.toBeInTheDocument()
})
