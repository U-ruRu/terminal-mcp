import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import type { ConsoleSnapshotReadModel, ManagedFleetControlReadModel, ManagedFleetMutationResult, PersistentMutationResult, PersistentSlotReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { saveCachedFleetControl } from '../connections/controlState'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerSlots, type FleetControlLoader, type FleetControlMutator, type PersistentMutator } from './ServerSlots'

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
        rearmAfterSeconds: 180,
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
        freshness: status === 'live' ? 'fresh' : 'stale',
        catchingUpScopes: [],
      },
    },
  }
}


function managedControl(legacyAdmissionEnabled = false, revision = 4): ManagedFleetControlReadModel {
  return {
    schemaVersion: 3,
    fleetId: 'fleet-a',
    nodeId: 'secondary',
    controlNodeId: 'main',
    managed: true,
    mesh: {
      meshId: 'mesh-prod',
      displayName: 'Production',
      adopted: true,
      adoptedAt: '2026-10-02T05:00:00Z',
      updatedAt: '2026-10-02T06:00:00Z',
    },
    meshes: [{
      meshId: 'mesh-prod',
      displayName: 'Production',
      adopted: true,
      adoptedAt: '2026-10-02T05:00:00Z',
      updatedAt: '2026-10-02T06:00:00Z',
    }],
    nodes: [{
      nodeId: 'secondary',
      origin: 'https://alpha.example',
      meshId: 'mesh-prod',
      state: 'active',
      desiredTopologyRevision: 2,
      appliedTopologyRevision: 2,
      desiredTrustRevision: 2,
      appliedTrustRevision: 2,
      desiredPolicyRevision: revision,
      appliedPolicyRevision: revision,
      updatedAt: '2026-10-02T06:00:00Z',
    }],
    policy: {
      durationSeconds: 1380,
      warningAfterSeconds: 1200,
      alertAfterSeconds: 1320,
      rearmAfterSeconds: 180,
      legacyAdmissionEnabled,
      revision,
      updatedAt: '2026-10-02T06:00:00Z',
    },
    revisions: { routing: 1, topology: 2, trust: 2, accessPolicy: revision },
    updatedAt: '2026-10-02T06:00:00Z',
  }
}

function renderSlots(
  fleet: FleetInstanceView[],
  mutatePersistent?: PersistentMutator,
  loadSlotAudit?: (instanceId: string, logicalAgentId: string) => Promise<import('../api/models').PersistentAuditReadModel[]>,
  loadFleetControl?: FleetControlLoader,
  mutateFleetControl?: FleetControlMutator,
) {
  const initialInstanceId = fleet[0]?.profile.instanceId ?? 'alpha'
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/servers/' + initialInstanceId + '/slots']}>
        <Routes>
          <Route path="/servers/:instanceId/slots" element={<ServerSlots instances={fleet} mutatePersistent={mutatePersistent} loadSlotAudit={loadSlotAudit} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
          <Route path="/servers/:instanceId/slots/:logicalAgentId" element={<ServerSlots instances={fleet} mutatePersistent={mutatePersistent} loadSlotAudit={loadSlotAudit} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
})

test('cached/offline read state does not disable a healthy authenticated write route', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('offline', slot())], mutate)
  expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
  expect(screen.getByRole('status')).toHaveTextContent('cached')
  await user.click(screen.getByRole('button', { name: 'Play' }))
  expect(mutate).toHaveBeenCalledWith(
    'alpha',
    '/actions/persistent/slots/play',
    expect.objectContaining({ logical_agent_id: 'la_alpha', expected_revision: 7, idempotency_key: expect.any(String) }),
  )
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

  expect(screen.getByText('Legacy selector')).toBeInTheDocument()
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
  expect(screen.queryByText('la_alpha')).not.toBeInTheDocument()
  expect(screen.getAllByText('Alpha Slot').length).toBeGreaterThan(0)
})


test('policy controls mutate D/W/A/R and Legacy through the authenticated write path', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  const duration = screen.getByRole('spinbutton', { name: 'D — hard duration (seconds)' })
  const warning = screen.getByRole('spinbutton', { name: 'W — warning after (seconds)' })
  const alert = screen.getByRole('spinbutton', { name: 'A — alert after (seconds)' })
  const rearm = screen.getByRole('spinbutton', { name: 'R — automatic rearm after (seconds)' })
  await user.clear(duration); await user.type(duration, '180')
  await user.clear(warning); await user.type(warning, '60')
  await user.clear(alert); await user.type(alert, '120')
  await user.clear(rearm); await user.type(rearm, '15')
  await user.click(screen.getByRole('button', { name: 'Save D/W/A/R' }))
  expect(mutate).toHaveBeenCalledWith('alpha', '/actions/persistent/policy', { duration_seconds: 180, warning_after_seconds: 60, alert_after_seconds: 120, rearm_after_seconds: 15 })

  await user.click(screen.getByRole('checkbox', { name: 'Allow Legacy agent admission' }))
  expect(mutate).toHaveBeenCalledWith('alpha', '/actions/persistent/policy', { legacy_admission_enabled: true })
})


test('managed AccessPolicy uses Fleet control authority for timing, Legacy and reset', async () => {
  const user = userEvent.setup()
  const persistentMutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  let revision = 4
  let policy = {
    durationSeconds: 1380,
    warningAfterSeconds: 1200,
    alertAfterSeconds: 1320,
    rearmAfterSeconds: 180,
    legacyAdmissionEnabled: false,
    revision,
    updatedAt: '2026-10-02T06:00:00Z',
  }
  const baseControl: ManagedFleetControlReadModel = {
    schemaVersion: 2,
    fleetId: 'fleet-a',
    nodeId: 'secondary',
    controlNodeId: 'main',
    managed: true,
    mesh: {
      meshId: 'mesh-a',
      displayName: 'Production',
      adopted: true,
      adoptedAt: '2026-10-02T05:00:00Z',
      updatedAt: '2026-10-02T06:00:00Z',
    },
    meshes: [{
      meshId: 'mesh-a',
      displayName: 'Production',
      adopted: true,
      adoptedAt: '2026-10-02T05:00:00Z',
      updatedAt: '2026-10-02T06:00:00Z',
    }],
    nodes: [{
      nodeId: 'secondary',
      origin: 'https://alpha.example',
      state: 'active',
      desiredTopologyRevision: 2,
      appliedTopologyRevision: 2,
      desiredTrustRevision: 2,
      appliedTrustRevision: 2,
      desiredPolicyRevision: 4,
      appliedPolicyRevision: 4,
      updatedAt: '2026-10-02T06:00:00Z',
    }],
    policy,
    revisions: { routing: 1, topology: 2, trust: 2, accessPolicy: 4 },
    updatedAt: '2026-10-02T06:00:00Z',
  }
  const loadControl = vi.fn(async () => ({ ...baseControl, policy })) as FleetControlLoader
  const mutateControl = vi.fn(async (_instanceId: string, path: string, body: Record<string, unknown>): Promise<ManagedFleetMutationResult> => {
    revision += 1
    if (path.endsWith('/reset')) {
      policy = {
        durationSeconds: 1380,
        warningAfterSeconds: 1200,
        alertAfterSeconds: 1320,
        rearmAfterSeconds: 180,
        legacyAdmissionEnabled: false,
        revision,
        updatedAt: '2026-10-02T06:01:00Z',
      }
    } else {
      policy = {
        durationSeconds: Number(body.duration_seconds),
        warningAfterSeconds: Number(body.warning_after_seconds),
        alertAfterSeconds: Number(body.alert_after_seconds),
        rearmAfterSeconds: Number(body.rearm_after_seconds),
        legacyAdmissionEnabled: Boolean(body.legacy_admission_enabled),
        revision,
        updatedAt: '2026-10-02T06:01:00Z',
      }
    }
    return {
      ok: true,
      control: {
        ...baseControl,
        policy,
        revisions: { ...baseControl.revisions, accessPolicy: revision },
      },
    }
  }) as FleetControlMutator

  renderSlots([instance('offline', slot('active', '2026-09-30T12:02:00Z'))], persistentMutate, undefined, loadControl, mutateControl)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reset to defaults' })).toBeEnabled())
  expect(screen.queryByText(/Suspend or cancel every armed\/active slot/)).not.toBeInTheDocument()

  const duration = screen.getByRole('spinbutton', { name: 'D — hard duration (seconds)' })
  const warning = screen.getByRole('spinbutton', { name: 'W — warning after (seconds)' })
  const alert = screen.getByRole('spinbutton', { name: 'A — alert after (seconds)' })
  const rearm = screen.getByRole('spinbutton', { name: 'R — automatic rearm after (seconds)' })
  await user.clear(duration); await user.type(duration, '180')
  await user.clear(warning); await user.type(warning, '60')
  await user.clear(alert); await user.type(alert, '120')
  await user.clear(rearm); await user.type(rearm, '15')
  await user.click(screen.getByRole('button', { name: 'Save D/W/A/R' }))

  expect(mutateControl).toHaveBeenNthCalledWith(1, 'alpha', '/actions/fleet/control/policy', {
    duration_seconds: 180,
    warning_after_seconds: 60,
    alert_after_seconds: 120,
    rearm_after_seconds: 15,
    legacy_admission_enabled: false,
    expected_revision: 4,
  })
  expect(persistentMutate).not.toHaveBeenCalled()

  await user.click(screen.getByRole('checkbox', { name: 'Allow Legacy agent admission' }))
  expect(mutateControl).toHaveBeenNthCalledWith(2, 'alpha', '/actions/fleet/control/policy', expect.objectContaining({
    legacy_admission_enabled: true,
    expected_revision: 5,
  }))
  await user.click(screen.getByRole('button', { name: 'Reset to defaults' }))
  expect(mutateControl).toHaveBeenNthCalledWith(3, 'alpha', '/actions/fleet/control/policy/reset', { expected_revision: 6 })
})

test('old server snapshots do not expose policy mutation controls', () => {
  const old = instance('live', slot())
  old.runtime.realtime!.snapshot!.persistent!.policy.policyControlSupported = false
  renderSlots([old])
  expect(screen.queryByRole('button', { name: 'Save D/W/A/R' })).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: 'Allow Legacy agent admission' })).not.toBeInTheDocument()
})

test('stale active state remains informative but does not client-side fence policy writes', () => {
  renderSlots([instance('live', slot('active', '2026-09-30T12:02:00Z'))], vi.fn(async () => ({ ok: true, payload: { ok: true } })) as PersistentMutator)
  expect(screen.getByRole('button', { name: 'Save D/W/A/R' })).toBeEnabled()
  expect(screen.getByRole('spinbutton', { name: 'D — hard duration (seconds)' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
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


test('Persistent Access code stays visible on the slot card, copies exactly, and rotates', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn()
    .mockResolvedValueOnce({
      ok: true,
      payload: {
        ok: true,
        access: { public_name: 'Alpha', access_generation: 1, access_code: '0042' },
      },
    })
    .mockResolvedValueOnce({
      ok: true,
      payload: {
        ok: true,
        access: { public_name: 'Alpha', access_generation: 2, access_code: '7319' },
      },
    }) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  expect(screen.queryByRole('button', { name: 'Copy selector' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Rotate selector' })).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Set up Access code' }))
  expect(mutate).toHaveBeenLastCalledWith(
    'alpha',
    '/actions/persistent/slots/migrate-access',
    { logical_agent_id: 'la_alpha' },
  )
  expect(screen.getByText('0042')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Copy Access code — Alpha Slot' })).toBeEnabled()
  expect(localStorage.getItem('terminal-mcp.console.access-code.v1.la_alpha')).toContain('0042')

  await user.click(screen.getByRole('button', { name: 'Rotate Access code' }))
  expect(mutate).toHaveBeenLastCalledWith(
    'alpha',
    '/actions/persistent/slots/rotate-access-code',
    { logical_agent_id: 'la_alpha' },
  )
  expect(screen.queryByText('0042')).not.toBeInTheDocument()
  expect(screen.getByText('7319')).toBeInTheDocument()
  expect(localStorage.getItem('terminal-mcp.console.access-code.v1.la_alpha')).toContain('7319')

  await user.click(screen.getByRole('link', { name: 'Details' }))
  expect(screen.getAllByText('7319').length).toBeGreaterThan(0)
})


test('existing Access generation without a local code offers rotation and stores the replacement', async () => {
  const user = userEvent.setup()
  const item = slot()
  item.access = { publicName: 'Alpha', accessGeneration: 3, status: 'active' }
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({
    ok: true,
    payload: {
      ok: true,
      access: { public_name: 'Alpha', access_generation: 4, access_code: '9007' },
    },
  })) as PersistentMutator
  renderSlots([instance('live', item)], mutate)

  expect(screen.getByText('Access code is not stored on this device. Rotate it to obtain a new local copy.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Rotate Access code' }))

  expect(screen.getByText('9007')).toBeInTheDocument()
  expect(localStorage.getItem('terminal-mcp.console.access-code.v1.la_alpha')).toContain('9007')
})


test('cached managed policy stays mesh-wide when the fresh control read is unavailable', async () => {
  const user = userEvent.setup()
  saveCachedFleetControl('alpha', managedControl(false), 100)
  const persistentMutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  const loadControl = vi.fn(async (): Promise<ManagedFleetControlReadModel> => { throw new Error('control_unavailable') }) as FleetControlLoader
  const mutateControl = vi.fn(async (_instanceId: string, _path: string, body: Record<string, unknown>): Promise<ManagedFleetMutationResult> => ({
    ok: true,
    control: managedControl(Boolean(body.legacy_admission_enabled), 5),
  })) as FleetControlMutator

  renderSlots([instance('live', slot())], persistentMutate, undefined, loadControl, mutateControl)

  const toggle = await screen.findByRole('checkbox', { name: 'Allow Legacy agent admission' })
  await waitFor(() => expect(screen.getByText('Policy scope: Managed mesh · Production · Stale')).toBeInTheDocument())
  expect(toggle).not.toBeChecked()
  await user.click(toggle)

  expect(mutateControl).toHaveBeenCalledWith('alpha', '/actions/fleet/control/policy', expect.objectContaining({
    legacy_admission_enabled: true,
    expected_revision: 4,
  }))
  expect(persistentMutate).not.toHaveBeenCalled()
  await waitFor(() => expect(toggle).toBeChecked())
})

test('Legacy toggle is optimistic and rolls back visibly when authoritative mutation is rejected', async () => {
  const user = userEvent.setup()
  let resolveMutation: ((value: ManagedFleetMutationResult) => void) | undefined
  const mutateControl = vi.fn(() => new Promise<ManagedFleetMutationResult>((resolve) => { resolveMutation = resolve })) as FleetControlMutator
  const loadControl = vi.fn(async () => managedControl(false)) as FleetControlLoader
  const persistentMutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator

  renderSlots([instance('live', slot())], persistentMutate, undefined, loadControl, mutateControl)

  const toggle = await screen.findByRole('checkbox', { name: 'Allow Legacy agent admission' })
  await waitFor(() => expect(toggle).toBeEnabled())
  expect(toggle).not.toBeChecked()

  await user.click(toggle)
  expect(toggle).toBeChecked()
  expect(screen.getByText('Pending')).toBeInTheDocument()

  resolveMutation?.({ ok: false, code: 'revision_conflict' })
  await waitFor(() => expect(toggle).not.toBeChecked())
  expect(screen.getByText('Failed')).toBeInTheDocument()
  expect(persistentMutate).not.toHaveBeenCalled()
})
