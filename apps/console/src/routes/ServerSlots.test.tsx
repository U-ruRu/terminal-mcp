import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import type { ConsoleSnapshotReadModel, PersistentMutationResult, PersistentSlotReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerSlots, type PersistentMutator } from './ServerSlots'

function slot(state: string = 'suspended', hardExpiresAt?: string): PersistentSlotReadModel {
  return {
    logicalAgentId: 'la_alpha',
    displayName: 'Alpha Slot',
    state,
    authorityNodeId: 'secondary',
    authorityEpoch: 4,
    slotRevision: 7,
    selector: 'A1B2',
    selectorGeneration: 2,
    authGeneration: 1,
    createdAt: '2026-09-30T11:00:00Z',
    updatedAt: '2026-09-30T11:30:00Z',
    serverNow: '2026-09-30T12:00:00Z',
    workSession: hardExpiresAt
      ? {
          workSessionId: 'ws_alpha',
          sessionEpoch: 3,
          authorityNodeId: 'secondary',
          authorityEpoch: 4,
          startedAt: '2026-09-30T11:39:00Z',
          hardExpiresAt,
          state: 'active',
        }
      : undefined,
    claims: [],
    audit: [],
    attachments: [],
  }
}

function snapshot(item: PersistentSlotReadModel): ConsoleSnapshotReadModel {
  return {
    highWaterSeq: 3,
    replayFromSeq: 3,
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
    agents: [],
    tasks: [],
    contexts: [],
    communications: [],
    persistent: {
      enabled: true,
      available: true,
      serverNow: '2026-09-30T12:00:00Z',
      policy: {
        durationSeconds: 1380,
        warningAfterSeconds: 1200,
        alertAfterSeconds: 1320,
        manualRearm: true,
        admissionMode: 'bearer',
        legacyAdmissionEnabled: false,
        policyControlSupported: true,
      },
      slots: [item],
    },
  }
}

function instance(status: 'live' | 'offline', item: PersistentSlotReadModel): FleetInstanceView {
  return {
    profile: {
      instanceId: 'alpha',
      origin: 'https://alpha.example',
      displayName: 'Alpha',
      credentialRef: 'cred-alpha',
      metadata: { deviceId: 'd', clientId: 'c', deviceLabel: 'Console', scope: 'terminal:read terminal:execute', pairedAt: 1 },
      createdAt: 1,
      updatedAt: 1,
    },
    runtime: {
      instanceId: 'alpha',
      status,
      authStatus: 'connected',
      reconnectAttempt: 0,
      realtime: {
        status,
        snapshot: snapshot(item),
        cursor: 3,
        highWaterSeq: 3,
        socketConnected: status === 'live',
        reconnectAttempt: 0,
      },
    },
  }
}

function renderSlots(
  fleet: FleetInstanceView[],
  mutatePersistent?: PersistentMutator,
  loadSlotAudit?: (instanceId: string, logicalAgentId: string) => Promise<import('../api/models').PersistentAuditReadModel[]>,
) {
  const initialInstanceId = fleet[0]?.profile.instanceId ?? 'alpha'
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/' + initialInstanceId + '/slots']}>
        <Routes>
          <Route path="/servers/:instanceId/slots" element={<ServerSlots instances={fleet} mutatePersistent={mutatePersistent} loadSlotAudit={loadSlotAudit} />} />
          <Route path="/servers/:instanceId/slots/:logicalAgentId" element={<ServerSlots instances={fleet} mutatePersistent={mutatePersistent} loadSlotAudit={loadSlotAudit} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

test('cached/offline slot state stays readable but mutations are disabled', () => {
  renderSlots([instance('offline', slot())])
  expect(screen.getByText('A1B2')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Copy' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Play' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('cached')
})

test('delete requires explicit confirmation and only succeeds through a live authority mutation', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({
    ok: true,
    payload: { ok: true },
  })) as PersistentMutator
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
  renderSlots([instance('live', slot())], mutate)

  await user.click(screen.getByRole('button', { name: 'Delete' }))
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(mutate).not.toHaveBeenCalled()

  await user.click(screen.getByRole('button', { name: 'Delete' }))
  expect(confirm).toHaveBeenCalledTimes(2)
  expect(mutate).toHaveBeenCalledWith(
    'alpha',
    '/actions/persistent/slots/delete',
    expect.objectContaining({ logical_agent_id: 'la_alpha', expected_revision: 7, idempotency_key: expect.any(String) }),
  )
})

test('authority clock drives a non-color warning cue near hard expiry', async () => {
  renderSlots([instance('live', slot('active', '2026-09-30T12:02:00Z'))])
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Warning'))
  expect(screen.getByRole('status').closest('.slot-card')).toHaveClass('slot-cue-warning')
})

test('expanded slot detail exposes session, generations and admission policy', async () => {
  const user = userEvent.setup()
  renderSlots([instance('live', slot('active', '2026-09-30T12:02:00Z'))])
  await user.click(screen.getByRole('link', { name: 'Details' }))

  expect(screen.getByText('Selector generation')).toBeInTheDocument()
  expect(screen.getByText('Auth generation')).toBeInTheDocument()
  expect(screen.getByText('Session epoch')).toBeInTheDocument()
  expect(screen.getByText('Hard expiry')).toBeInTheDocument()
  expect(screen.getByText('Manual rearm')).toBeInTheDocument()
  expect(screen.getByText('Admission mode')).toBeInTheDocument()
  expect(screen.getByText('Legacy admission')).toBeInTheDocument()
  expect(screen.getByText('ws_alpha')).toBeInTheDocument()
  expect(screen.getByText('2026-09-30T12:02:00Z')).toBeInTheDocument()
  expect(screen.getByText('bearer')).toBeInTheDocument()
})


test('policy controls mutate D/W/A and Legacy only on capable live servers', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  const duration = screen.getByRole('spinbutton', { name: 'D — hard duration (seconds)' })
  const warning = screen.getByRole('spinbutton', { name: 'W — warning after (seconds)' })
  const alert = screen.getByRole('spinbutton', { name: 'A — alert after (seconds)' })
  await user.clear(duration); await user.type(duration, '180')
  await user.clear(warning); await user.type(warning, '60')
  await user.clear(alert); await user.type(alert, '120')
  await user.click(screen.getByRole('button', { name: 'Save D/W/A' }))
  expect(mutate).toHaveBeenCalledWith('alpha', '/actions/persistent/policy', { duration_seconds: 180, warning_after_seconds: 60, alert_after_seconds: 120 })

  await user.click(screen.getByRole('checkbox', { name: 'Allow Legacy agent admission' }))
  expect(mutate).toHaveBeenCalledWith('alpha', '/actions/persistent/policy', { legacy_admission_enabled: true })
})

test('old server snapshots do not expose policy mutation controls', () => {
  const old = instance('live', slot())
  old.runtime.realtime!.snapshot!.persistent!.policy.policyControlSupported = false
  renderSlots([old])
  expect(screen.queryByRole('button', { name: 'Save D/W/A' })).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: 'Allow Legacy agent admission' })).not.toBeInTheDocument()
})

test('D/W/A controls lock while a slot is active but Legacy remains switchable', () => {
  renderSlots([instance('live', slot('active', '2026-09-30T12:02:00Z'))], vi.fn(async () => ({ ok: true, payload: { ok: true } })) as PersistentMutator)
  expect(screen.getByRole('button', { name: 'Save D/W/A' })).toBeDisabled()
  expect(screen.getByRole('spinbutton', { name: 'D — hard duration (seconds)' })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: 'Allow Legacy agent admission' })).toBeEnabled()
  expect(screen.getByText(/Suspend or cancel every armed\/active slot/)).toBeInTheDocument()
})


test('unpaired projected slot can load audit through Fleet ingress while mutations stay disabled', async () => {
  const user = userEvent.setup()
  const projected = instance('live', slot())
  projected.profile = { ...projected.profile, instanceId: 'fleet-source-node-b', displayName: 'Node B' }
  projected.runtime = { ...projected.runtime, instanceId: 'fleet-source-node-b', authStatus: 'unpaired' }
  const loadAudit = vi.fn(async () => [{
    id: 99,
    eventType: 'session_start',
    principalId: 'fleet-ingress',
    payload: {},
    createdAt: '2026-09-30T12:01:00Z',
  }])
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator

  renderSlots([projected], mutate, loadAudit)
  await user.click(screen.getByRole('link', { name: 'Details' }))

  await waitFor(() => expect(loadAudit).toHaveBeenCalledWith('fleet-source-node-b', 'la_alpha'))
  expect(screen.getByText('session_start')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Play' })).toBeDisabled()
  expect(mutate).not.toHaveBeenCalled()
})
