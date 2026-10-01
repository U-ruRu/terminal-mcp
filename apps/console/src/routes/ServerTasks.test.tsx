import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import type { ConsoleSnapshotReadModel, TaskReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerTasks } from './ServerTasks'

const task: TaskReadModel = {
  key: 'console/T-1', namespace: 'console', taskId: 'T-1', title: 'Clickable task',
  lane: 'implementation', priority: 'P1', state: 'ready', operationalStatus: 'ready',
  active: false, archived: false, tags: [], nextAction: 'Open me', participants: [], checkpoint: {},
}

const snapshot: ConsoleSnapshotReadModel = {
  highWaterSeq: 1, replayFromSeq: 1, duplicateEventsPossible: true,
  instance: {
    application: 'terminal-mcp', version: '0.10.1', publicBaseUrl: 'https://alpha.example', healthy: true, health: {},
    resources: { status: 'unavailable', cpu: { status: 'unavailable' }, memory: { status: 'unavailable' }, filesystem: { status: 'unavailable' }, uptime: { status: 'unavailable' } },
  },
  agents: [], tasks: [task], contexts: [], communications: [],
}

const instance: FleetInstanceView = {
  profile: { instanceId: 'alpha', origin: 'https://alpha.example', displayName: 'Alpha', credentialRef: 'cred-alpha', metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
  runtime: { instanceId: 'alpha', status: 'live', authStatus: 'connected', reconnectAttempt: 0, realtime: { status: 'live', snapshot, cursor: 1, highWaterSeq: 1, socketConnected: true, reconnectAttempt: 0 } },
}

afterEach(() => cleanup())

test('the whole task card opens task detail', async () => {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/alpha/tasks']}>
        <Routes>
          <Route path="/servers/:instanceId/tasks" element={<ServerTasks instances={[instance]} />} />
          <Route path="/servers/:instanceId/tasks/:namespace/:taskId" element={<div>Task destination</div>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )

  const card = screen.getByRole('link', { name: 'T-1 · Clickable task' })
  expect(card).toHaveAttribute('href', '/servers/alpha/tasks/console/T-1')
  await userEvent.click(card)
  expect(screen.getByText('Task destination')).toBeInTheDocument()
})


test('unpaired projected task detail loads through Fleet ingress', async () => {
  const projected: FleetInstanceView = {
    profile: { ...instance.profile, instanceId: 'fleet-source-node-b', displayName: 'Node B' },
    runtime: {
      ...instance.runtime,
      instanceId: 'fleet-source-node-b',
      authStatus: 'unpaired',
      realtime: instance.runtime.realtime
        ? { ...instance.runtime.realtime, snapshot: { ...snapshot, tasks: [] } }
        : null,
    },
  }
  const loaded: TaskReadModel = { ...task, key: 'console/T-9', taskId: 'T-9', title: 'Remote cold detail' }
  const loadTask = vi.fn(async () => loaded)
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/fleet-source-node-b/tasks/console/T-9']}>
        <Routes>
          <Route path="/servers/:instanceId/tasks/:namespace/:taskId" element={<ServerTasks instances={[projected]} loadTask={loadTask} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )

  await waitFor(() => expect(loadTask).toHaveBeenCalledWith('fleet-source-node-b', 'console', 'T-9'))
  expect(await screen.findByText(/T-9 · Remote cold detail/)).toBeInTheDocument()
})
