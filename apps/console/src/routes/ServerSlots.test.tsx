import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { useState } from 'react'
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


function authorityInstance(): FleetInstanceView {
  const value = instance('live', slot())
  value.profile = { ...value.profile, instanceId: 'main', origin: 'https://main.example', displayName: 'Main', credentialRef: 'cred-main' }
  value.runtime = { ...value.runtime, instanceId: 'main', authStatus: 'connected', status: 'live' }
  return value
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

function standaloneControl(instanceId: string): ManagedFleetControlReadModel {
  const base = managedControl(false)
  return {
    ...base,
    nodeId: instanceId,
    controlNodeId: instanceId,
    managed: false,
    mesh: undefined,
    meshes: [],
    nodes: [],
    policy: undefined,
    updatedAt: '2026-10-03T08:00:00Z',
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
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

test('server-scoped slots derive Server context from the route without a second selector', () => {
  renderSlots([instance('live', slot())])
  expect(screen.queryByRole('combobox', { name: 'Switch server' })).not.toBeInTheDocument()
})

test('cached/offline read state does not disable a healthy authenticated write route', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('offline', slot())], mutate)
  expect(screen.getByText('Paused')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Start' })).toHaveClass('ui-icon-button', 'ui-button-primary')
  expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
  const statusBadge = screen.getByRole('status')
  expect(statusBadge).toHaveTextContent('Offline')
  expect(statusBadge).toHaveClass('status', 'server-status')
  expect(statusBadge).toHaveAttribute('title', expect.stringContaining('cached'))
  await user.click(screen.getByRole('button', { name: 'Start' }))
  expect(mutate).toHaveBeenCalledWith(
    'alpha',
    '/actions/persistent/slots/play',
    expect.objectContaining({ logical_agent_id: 'la_alpha', expected_revision: 7, idempotency_key: expect.any(String) }),
  )
})

test('mutation confirmation is a top transient toast that auto-dismisses within three seconds', async () => {
  vi.useFakeTimers()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  fireEvent.click(screen.getByRole('button', { name: 'Start' }))
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  const toast = screen.getByText('Change acknowledged by authority.')
  expect(toast.closest('.transient-toast')).toBeInTheDocument()
  act(() => vi.advanceTimersByTime(2600))
  expect(toast.closest('.transient-toast')).toHaveClass('transient-toast-exit')
  act(() => vi.advanceTimersByTime(400))
  expect(screen.queryByText('Change acknowledged by authority.')).not.toBeInTheDocument()
})

test('mutation toast supports horizontal swipe dismissal', async () => {
  vi.useFakeTimers()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  fireEvent.click(screen.getByRole('button', { name: 'Start' }))
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  const toast = screen.getByText('Change acknowledged by authority.').closest('.transient-toast') as HTMLElement
  fireEvent.pointerDown(toast, { clientX: 120 })
  fireEvent.pointerUp(toast, { clientX: 190 })
  expect(toast).toHaveClass('transient-toast-exit')
  act(() => vi.advanceTimersByTime(220))
  expect(screen.queryByText('Change acknowledged by authority.')).not.toBeInTheDocument()
})

test('slot card groups the lifecycle and overflow controls at the trailing edge', () => {
  renderSlots([instance('live', slot())])
  const card = screen.getByRole('link', { name: /Alpha Slot/ }).closest('.slot-card') as HTMLElement
  const actions = card.querySelector('.slot-card-actions') as HTMLElement
  expect(actions).toBeInTheDocument()
  expect(within(actions).getByRole('button', { name: 'Start' })).toBeInTheDocument()
  expect(actions.querySelector('.slot-more-actions')).toBeInTheDocument()
  expect(card.querySelector('.slot-code-cell')?.nextElementSibling).toBe(actions)
})

test('slot overflow flips above when space below is constrained and closes on outside press', () => {
  renderSlots([instance('live', slot())])
  const details = document.querySelector('.slot-more-actions') as HTMLDetailsElement
  const summary = details.querySelector('summary') as HTMLElement
  const menu = details.querySelector('.slot-overflow-menu') as HTMLElement
  const nav = document.createElement('nav')
  nav.className = 'mobile-bottom-navigation'
  document.body.appendChild(nav)
  const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this === summary) return { top: 620, bottom: 664, left: 0, right: 44, width: 44, height: 44, x: 0, y: 620, toJSON: () => ({}) } as DOMRect
    if (this === menu) return { top: 668, bottom: 868, left: 0, right: 220, width: 220, height: 200, x: 0, y: 668, toJSON: () => ({}) } as DOMRect
    if (this === nav) return { top: 700, bottom: 764, left: 0, right: 390, width: 390, height: 64, x: 0, y: 700, toJSON: () => ({}) } as DOMRect
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
  })
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(0); return 1 })

  details.open = true
  fireEvent(details, new Event('toggle'))
  expect(details).toHaveClass('slot-more-actions-up')
  fireEvent.pointerDown(document.body)
  expect(details.open).toBe(false)
  rect.mockRestore()
  nav.remove()
})

test('delete requires explicit confirmation and only succeeds through a live authority mutation', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({
    ok: true,
    payload: { ok: true },
  })) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  await user.click(screen.getByRole('button', { name: 'Delete' }))
  expect(screen.getByRole('dialog', { name: 'Delete' })).toBeInTheDocument()
  expect(mutate).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.queryByRole('dialog', { name: 'Delete' })).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Delete' }))
  const dialog = screen.getByRole('dialog', { name: 'Delete' })
  await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
  expect(mutate).toHaveBeenCalledWith(
    'alpha',
    '/actions/persistent/slots/delete',
    expect.objectContaining({ logical_agent_id: 'la_alpha', expected_revision: 7, idempotency_key: expect.any(String) }),
  )
})

test('authority clock drives a non-color warning cue near hard expiry', async () => {
  renderSlots([instance('live', slot('active', '2026-09-30T12:02:00Z'))])
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('2m 00s'))
  expect(screen.getByRole('status').closest('.slot-card')).toHaveClass('slot-cue-warning')
})

test('expanded slot detail exposes session, generations and admission policy', async () => {
  const user = userEvent.setup()
  const detailSlot = slot('active', '2026-09-30T12:02:00Z')
  detailSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  renderSlots([instance('live', detailSlot)])
  await user.click(screen.getByRole('link', { name: 'Details' }))
  expect(screen.getByText('Technical details')).toBeInTheDocument()

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
  expect(screen.getAllByText('Alpha · Alpha Slot').length).toBeGreaterThan(0)
})


test('slot detail keeps raw Fleet and audit identifiers inside technical details and rotation secondary', async () => {
  const user = userEvent.setup()
  const item = slot()
  item.access = { publicName: 'Alpha Slot', accessGeneration: 2, status: 'active' }
  item.attachments = [{ nodeAttachmentId: 'attach-secret-42', nodeInstanceId: 'secondary', attachedAt: '2026-09-30T11:00:00Z', hardExpiresAt: '2026-09-30T12:30:00Z' }]
  item.audit = [{ id: 77, eventType: 'slot.played', principalId: 'principal-secret-77', payload: {}, createdAt: '2026-09-30T11:10:00Z' }]
  renderSlots([instance('live', item)])

  await user.click(screen.getByRole('link', { name: 'Details' }))
  const technical = screen.getByText('Technical details').closest('details') as HTMLElement
  expect(technical).toContainElement(screen.getByText('attach-secret-42'))
  expect(technical).toContainElement(screen.getByText('principal-secret-77'))
  expect(technical).toContainElement(screen.getByText('#77'))
  expect(technical).toContainElement(screen.getByText('slot.played'))
  const detail = technical.closest('.slot-detail') as HTMLElement
  expect(within(detail).getByText('Slot made available')).toBeInTheDocument()
  expect(within(detail).getByRole('button', { name: 'Rotate Access code' })).toHaveClass('ui-icon-button', 'ui-button-secondary')
})

test('policy controls stage D/W/A/R and Legacy together and Save commits one standalone mutation', async () => {
  const user = userEvent.setup()
  const mutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  renderSlots([instance('live', slot())], mutate)

  const duration = screen.getByRole('spinbutton', { name: 'Session duration' })
  const warning = screen.getByRole('spinbutton', { name: 'Warning time' })
  const alert = screen.getByRole('spinbutton', { name: 'Alert time' })
  const rearm = screen.getByRole('spinbutton', { name: 'Rearm delay' })
  const legacy = screen.getByRole('checkbox', { name: 'Allow agents to create sessions independently' })
  await user.clear(duration); await user.type(duration, '3')
  await user.clear(warning); await user.type(warning, '1')
  await user.clear(alert); await user.type(alert, '2')
  await user.clear(rearm); await user.type(rearm, '15'); await user.selectOptions(screen.getByRole('combobox', { name: 'Rearm delay · Unit' }), 'seconds')
  await user.click(legacy)
  expect(legacy).toBeChecked()
  expect(mutate).not.toHaveBeenCalled()

  expect(screen.getByRole('button', { name: 'Save' })).toHaveClass('ui-icon-button', 'ui-button-primary')
  expect(screen.getByRole('button', { name: 'Create slot' })).toHaveClass('ui-icon-button', 'ui-button-primary')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  expect(mutate).toHaveBeenCalledTimes(1)
  expect(mutate).toHaveBeenCalledWith('alpha', '/actions/persistent/policy', {
    duration_seconds: 180,
    warning_after_seconds: 60,
    alert_after_seconds: 120,
    rearm_after_seconds: 15,
    legacy_admission_enabled: true,
  })

  await user.click(screen.getByRole('button', { name: 'Reset' }))
  expect(mutate).toHaveBeenNthCalledWith(2, 'alpha', '/actions/persistent/policy', {
    duration_seconds: 1380,
    warning_after_seconds: 1200,
    alert_after_seconds: 1320,
    rearm_after_seconds: 180,
    legacy_admission_enabled: false,
  })
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

  renderSlots([instance('offline', slot('active', '2026-09-30T12:02:00Z')), authorityInstance()], persistentMutate, undefined, loadControl, mutateControl)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reset' })).toBeEnabled())
  expect(screen.queryByText(/Suspend or pause every ready or active Slot/)).not.toBeInTheDocument()

  const duration = screen.getByRole('spinbutton', { name: 'Session duration' })
  const warning = screen.getByRole('spinbutton', { name: 'Warning time' })
  const alert = screen.getByRole('spinbutton', { name: 'Alert time' })
  const rearm = screen.getByRole('spinbutton', { name: 'Rearm delay' })
  await user.clear(duration); await user.type(duration, '3')
  await user.clear(warning); await user.type(warning, '1')
  await user.clear(alert); await user.type(alert, '2')
  await user.clear(rearm); await user.type(rearm, '15'); await user.selectOptions(screen.getByRole('combobox', { name: 'Rearm delay · Unit' }), 'seconds')
  await user.click(screen.getByRole('checkbox', { name: 'Allow agents to create sessions independently' }))
  expect(mutateControl).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Save' }))

  expect(mutateControl).toHaveBeenNthCalledWith(1, 'main', '/actions/fleet/control/policy', {
    duration_seconds: 180,
    warning_after_seconds: 60,
    alert_after_seconds: 120,
    rearm_after_seconds: 15,
    legacy_admission_enabled: true,
    expected_revision: 4,
  })
  expect(persistentMutate).not.toHaveBeenCalled()

  await user.click(screen.getByRole('button', { name: 'Reset' }))
  expect(mutateControl).toHaveBeenNthCalledWith(2, 'main', '/actions/fleet/control/policy/reset', { expected_revision: 5 })
})

test('old server snapshots do not expose policy mutation controls', () => {
  const old = instance('live', slot())
  old.runtime.realtime!.snapshot!.persistent!.policy.policyControlSupported = false
  renderSlots([old])
  expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: 'Allow agents to create sessions independently' })).not.toBeInTheDocument()
})

test('stale active state remains informative but does not client-side fence policy writes', () => {
  renderSlots([instance('live', slot('active', '2026-09-30T12:02:00Z'))], vi.fn(async () => ({ ok: true, payload: { ok: true } })) as PersistentMutator)
  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  expect(screen.getByRole('spinbutton', { name: 'Session duration' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
  expect(screen.getByRole('checkbox', { name: 'Allow agents to create sessions independently' })).toBeEnabled()
  expect(screen.getByText(/Suspend or pause every ready or active Slot/)).toBeInTheDocument()
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
  expect(screen.getByText('Session started')).toBeInTheDocument()
  const technical = screen.getByText('Technical details').closest('details') as HTMLElement
  expect(technical).toContainElement(screen.getByText('session_start'))
  expect(screen.getByRole('button', { name: 'Set up Access code' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument()
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
  expect(screen.getByRole('button', { name: /Copy Access code/ })).toBeEnabled()
  expect(localStorage.getItem('terminal-mcp.console.access-code.v1.la_alpha')).toContain('0042')

  await user.click(screen.getByRole('button', { name: 'Rotate Access code' }))
  const rotateDialog = screen.getByRole('dialog', { name: 'Rotate Access code' })
  await user.click(within(rotateDialog).getByRole('button', { name: 'Rotate Access code' }))
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

  expect(screen.queryByText('Access code is not stored on this device. Rotate it to obtain a new local copy.')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Rotate Access code' }))
  await user.click(within(screen.getByRole('dialog', { name: 'Rotate Access code' })).getByRole('button', { name: 'Rotate Access code' }))

  expect(screen.getByText('9007')).toBeInTheDocument()
  expect(localStorage.getItem('terminal-mcp.console.access-code.v1.la_alpha')).toContain('9007')
})


test('cached managed policy stays mesh-wide when the fresh control read is unavailable', async () => {
  const user = userEvent.setup()
  saveCachedFleetControl('main-profile', managedControl(false), 100)
  const persistentMutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator
  const loadControl = vi.fn(async (): Promise<ManagedFleetControlReadModel> => { throw new Error('control_unavailable') }) as FleetControlLoader
  const mutateControl = vi.fn(async (_instanceId: string, _path: string, body: Record<string, unknown>): Promise<ManagedFleetMutationResult> => ({
    ok: true,
    control: managedControl(Boolean(body.legacy_admission_enabled), 5),
  })) as FleetControlMutator

  renderSlots([instance('live', slot()), authorityInstance()], persistentMutate, undefined, loadControl, mutateControl)

  const toggle = await screen.findByRole('checkbox', { name: 'Allow agents to create sessions independently' })
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Slot settings' })).toBeInTheDocument())
  expect(screen.queryByText(/Policy scope:/)).not.toBeInTheDocument()
  expect(toggle).not.toBeChecked()
  await user.click(toggle)
  expect(toggle).toBeChecked()
  expect(mutateControl).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Save' }))

  expect(mutateControl).toHaveBeenCalledWith('main', '/actions/fleet/control/policy', expect.objectContaining({
    legacy_admission_enabled: true,
    expected_revision: 4,
  }))
  expect(persistentMutate).not.toHaveBeenCalled()
})

test('Legacy checkbox is draft-only and a rejected Save keeps the draft available for retry', async () => {
  const user = userEvent.setup()
  let resolveMutation: ((value: ManagedFleetMutationResult) => void) | undefined
  const mutateControl = vi.fn(() => new Promise<ManagedFleetMutationResult>((resolve) => { resolveMutation = resolve })) as FleetControlMutator
  const loadControl = vi.fn(async () => managedControl(false)) as FleetControlLoader
  const persistentMutate = vi.fn(async (): Promise<PersistentMutationResult> => ({ ok: true, payload: { ok: true } })) as PersistentMutator

  renderSlots([instance('live', slot()), authorityInstance()], persistentMutate, undefined, loadControl, mutateControl)

  const toggle = await screen.findByRole('checkbox', { name: 'Allow agents to create sessions independently' })
  await waitFor(() => expect(toggle).toBeEnabled())
  expect(toggle).not.toBeChecked()

  await user.click(toggle)
  expect(toggle).toBeChecked()
  expect(mutateControl).not.toHaveBeenCalled()

  await user.click(screen.getByRole('button', { name: 'Save' }))
  expect(mutateControl).toHaveBeenCalledTimes(1)
  resolveMutation?.({ ok: false, code: 'revision_conflict' })
  await waitFor(() => expect(screen.getByText('revision_conflict')).toBeInTheDocument())
  expect(toggle).toBeChecked()
  expect(persistentMutate).not.toHaveBeenCalled()
})

test('slot creation exposes and stores the issued Access code in the same flow', async () => {
  const user = userEvent.setup()
  const clipboard = vi.spyOn(navigator.clipboard, 'writeText')
  const mutate = vi.fn(async (_instanceId: string, path: string): Promise<PersistentMutationResult> => {
    if (path === '/actions/persistent/slots/create') {
      return {
        ok: true,
        payload: {
          ok: true,
          slot: { logical_agent_id: 'la_new' },
          access: { public_name: 'Builder', access_generation: 1, access_code: '4821' },
        },
      }
    }
    return { ok: true, payload: { ok: true } }
  }) as PersistentMutator

  renderSlots([instance('live', slot())], mutate)

  await user.click(screen.getByRole('button', { name: 'Create slot' }))

  await waitFor(() => expect(screen.getByText('4821')).toBeInTheDocument())
  expect(screen.getByRole('button', { name: 'Copy Access code — Builder' })).toBeEnabled()
  expect(localStorage.getItem('terminal-mcp.console.access-code.v1.la_new')).toContain('4821')
  expect(mutate).toHaveBeenCalledWith('alpha', '/actions/persistent/slots/create', { display_name: '' })
  expect(clipboard).toHaveBeenCalledWith('4821')
})

test('managed Mesh exposes Persistent slots without a physical-server switcher', () => {
  saveCachedFleetControl('alpha', managedControl(), Date.now())
  const meshSlot = slot()
  meshSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/meshes/mesh-prod/persistent']}>
        <Routes>
          <Route path="/meshes/:meshId/persistent" element={<ServerSlots instances={[instance('live', meshSlot)]} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )

  expect(screen.queryByRole('combobox', { name: 'Switch server' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Alpha.*Alpha Slot/ })).toHaveAttribute('href', '/meshes/mesh-prod/persistent/la_alpha')
  expect(screen.queryByRole('link', { name: 'Mesh' })).not.toBeInTheDocument()
})

test('unknown Persistent state has a human fallback while raw state stays diagnostic', async () => {
  const user = userEvent.setup()
  renderSlots([instance('live', slot('future_backend_state'))])
  expect(screen.getByText('State unavailable')).toBeInTheDocument()
  expect(screen.queryByText('future_backend_state')).not.toBeInTheDocument()

  await user.click(screen.getByRole('link', { name: 'Details' }))
  expect(screen.getByText('Technical details')).toBeInTheDocument()
  expect(screen.getByText('future_backend_state')).toBeInTheDocument()
})

test('global Slots aggregates authoritative standalone contexts while filter and settings target stay independent', async () => {
  const user = userEvent.setup()
  const alphaSlot = slot(); alphaSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const alpha = instance('live', alphaSlot)
  const betaSlot = { ...slot(), logicalAgentId: 'la_beta', displayName: 'Beta Slot', authorityNodeId: 'beta', access: { publicName: 'Beta', accessGeneration: 1, status: 'active' } }
  const beta = instance('live', betaSlot)
  beta.profile = { ...beta.profile, instanceId: 'beta', origin: 'https://beta.example', displayName: 'Beta', credentialRef: 'cred-beta' }
  beta.runtime = { ...beta.runtime, instanceId: 'beta' }
  const loadControl = vi.fn(async (instanceId: string) => instanceId === 'beta' ? standaloneControl('beta') : standaloneControl('alpha'))

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/slots']}>
        <Routes>
          <Route path="/slots" element={<ServerSlots instances={[alpha, beta]} loadFleetControl={loadControl} />} />
          <Route path="/slots/:logicalAgentId" element={<ServerSlots instances={[alpha, beta]} loadFleetControl={loadControl} />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  )

  const filter = await screen.findByRole('combobox', { name: 'Filter' })
  expect(filter).toHaveValue('all')
  expect(screen.getByRole('link', { name: /Alpha.*Alpha Slot/ })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Beta.*Beta Slot/ })).toBeInTheDocument()
  expect(await screen.findByRole('combobox', { name: 'Applies to' })).toHaveValue('server:alpha')

  await user.selectOptions(filter, 'server:beta')
  expect(screen.queryByRole('link', { name: /Alpha.*Alpha Slot/ })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Beta.*Beta Slot/ })).toHaveAttribute('href', '/slots/la_beta?filter=server%3Abeta&settings=server%3Aalpha&owner=server%3Abeta')
  expect(screen.getByRole('combobox', { name: 'Applies to' })).toHaveValue('server:alpha')
})


test.each([
  ['2026-09-30T12:00:42Z', '42s'],
  ['2026-09-30T12:12:08Z', '12m 08s'],
  ['2026-09-30T15:04:09Z', '3h 04m 09s'],
  ['2026-10-01T15:12:03Z', '27h 12m 03s'],
])('session timer keeps significant hours/minutes and always seconds: %s', (hardExpiresAt, expected) => {
  const item = slot('active', hardExpiresAt)
  item.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const value = instance('live', item)
  value.runtime.realtime!.snapshot!.persistent!.policy.durationSeconds = 200000
  value.runtime.realtime!.snapshot!.persistent!.policy.warningAfterSeconds = 190000
  value.runtime.realtime!.snapshot!.persistent!.policy.alertAfterSeconds = 195000
  renderSlots([value])
  const timer = screen.getByText(expected)
  expect(timer).toHaveClass('slot-timer')
  expect(timer.closest('.slot-card-secondary')).toBeInTheDocument()
})

test('global All keeps healthy owner Slots visible when another context is unavailable', async () => {
  const alphaSlot = slot(); alphaSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const alpha = instance('live', alphaSlot)
  const betaSlot = { ...slot(), logicalAgentId: 'la_beta', displayName: 'Beta Slot', authorityNodeId: 'beta', access: { publicName: 'Beta', accessGeneration: 1, status: 'active' } }
  const beta = instance('offline', betaSlot)
  beta.profile = { ...beta.profile, instanceId: 'beta', origin: 'https://beta.example', displayName: 'Beta', credentialRef: 'cred-beta' }
  beta.runtime = { ...beta.runtime, instanceId: 'beta' }
  beta.runtime.realtime!.snapshot!.persistent!.available = false
  beta.runtime.realtime!.snapshot!.persistent!.error = 'beta_down'
  const loadControl = vi.fn(async (instanceId: string) => standaloneControl(instanceId))

  render(<I18nProvider><MemoryRouter initialEntries={['/slots']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={[alpha, beta]} loadFleetControl={loadControl} />} />
  </Routes></MemoryRouter></I18nProvider>)

  expect(await screen.findByRole('link', { name: /Alpha.*Alpha Slot/ })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /Beta.*Beta Slot/ })).not.toBeInTheDocument()
  await waitFor(() => expect(screen.getAllByText('Beta').length).toBeGreaterThan(0))
  expect(screen.getByText(/beta_down/)).toBeInTheDocument()
})

test('each global Slot timer cue uses its owner policy, not the selected settings context', async () => {
  const alphaSlot = slot('active', '2026-09-30T12:00:30Z'); alphaSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const alpha = instance('live', alphaSlot)
  alpha.runtime.realtime!.snapshot!.persistent!.policy = { ...alpha.runtime.realtime!.snapshot!.persistent!.policy, durationSeconds: 100, warningAfterSeconds: 40, alertAfterSeconds: 80 }
  const betaSlot = { ...slot('active', '2026-09-30T12:09:00Z'), logicalAgentId: 'la_beta', displayName: 'Beta Slot', authorityNodeId: 'beta', access: { publicName: 'Beta', accessGeneration: 1, status: 'active' } }
  const beta = instance('live', betaSlot)
  beta.profile = { ...beta.profile, instanceId: 'beta', origin: 'https://beta.example', displayName: 'Beta', credentialRef: 'cred-beta' }
  beta.runtime = { ...beta.runtime, instanceId: 'beta' }
  beta.runtime.realtime!.snapshot!.persistent!.policy = { ...beta.runtime.realtime!.snapshot!.persistent!.policy, durationSeconds: 1000, warningAfterSeconds: 900, alertAfterSeconds: 950 }
  const loadControl = vi.fn(async (instanceId: string) => standaloneControl(instanceId))

  render(<I18nProvider><MemoryRouter initialEntries={['/slots']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={[alpha, beta]} loadFleetControl={loadControl} />} />
  </Routes></MemoryRouter></I18nProvider>)

  const alphaCard = (await screen.findByRole('link', { name: /Alpha.*Alpha Slot/ })).closest('.slot-card')
  const betaCard = screen.getByRole('link', { name: /Beta.*Beta Slot/ }).closest('.slot-card')
  expect(alphaCard).toHaveClass('slot-cue-warning')
  expect(betaCard).toHaveClass('slot-cue-normal')
  expect(await screen.findByRole('combobox', { name: 'Applies to' })).toHaveValue('server:alpha')
})

test('Mesh-owned Slot card and detail use managed control.policy over stale member persistent.policy', async () => {
  const user = userEvent.setup()
  const meshSlot = slot('active', '2026-09-30T12:05:00Z')
  meshSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const member = instance('live', meshSlot)
  member.runtime.realtime!.snapshot!.persistent!.policy = {
    ...member.runtime.realtime!.snapshot!.persistent!.policy,
    durationSeconds: 1200,
    warningAfterSeconds: 1000,
    alertAfterSeconds: 1100,
    rearmAfterSeconds: 300,
    legacyAdmissionEnabled: false,
  }
  const control = managedControl(true, 9)
  control.policy = {
    ...control.policy!,
    durationSeconds: 600,
    warningAfterSeconds: 240,
    alertAfterSeconds: 480,
    rearmAfterSeconds: 60,
    legacyAdmissionEnabled: true,
    revision: 9,
    updatedAt: '2026-10-03T11:00:00Z',
  }
  control.revisions = { ...control.revisions, accessPolicy: 9 }
  const loadControl = vi.fn(async () => control)

  render(<I18nProvider><MemoryRouter initialEntries={['/slots?filter=mesh%3Amesh-prod']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={[member]} loadFleetControl={loadControl} />} />
    <Route path="/slots/:logicalAgentId" element={<ServerSlots instances={[member]} loadFleetControl={loadControl} />} />
  </Routes></MemoryRouter></I18nProvider>)

  const identity = await screen.findByRole('link', { name: /Alpha.*Alpha Slot/ })
  const card = identity.closest('.slot-card') as HTMLElement
  expect(card).toHaveClass('slot-cue-warning')
  expect(within(card).getByText(/^(?:5m 00s|4m 59s)$/)).toBeInTheDocument()

  await user.click(identity)
  await user.click(await screen.findByText('Technical details'))
  const policyLabel = screen.getByText('Session policy')
  expect(policyLabel.parentElement?.textContent).toContain('Session duration 10 min')
  expect(policyLabel.parentElement?.textContent).toContain('Warning time 4 min')
  expect(policyLabel.parentElement?.textContent).toContain('Alert time 8 min')
  expect(policyLabel.parentElement?.textContent).toContain('Rearm delay 1 min')
  expect(screen.getByText('Legacy admission').parentElement?.textContent).toContain('Yes')
  expect(screen.getByText('Manual rearm').parentElement?.textContent).toContain('Unavailable')
  expect(screen.getByText('Admission mode').parentElement?.textContent).toContain('Unavailable')
})

test('cold-start authoritative topology does not expose Mesh members as standalone filters', async () => {
  const alphaSlot = slot(); alphaSlot.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const alpha = instance('live', alphaSlot)
  const beta = instance('live', { ...slot(), logicalAgentId: 'la_beta', displayName: 'Beta Slot', authorityNodeId: 'beta', access: { publicName: 'Beta', accessGeneration: 1, status: 'active' } })
  beta.profile = { ...beta.profile, instanceId: 'beta', origin: 'https://beta.example', displayName: 'Beta', credentialRef: 'cred-beta' }
  beta.runtime = { ...beta.runtime, instanceId: 'beta' }
  const control = managedControl()
  control.nodes = [
    { ...control.nodes[0], nodeId: 'secondary', origin: 'https://alpha.example', meshId: 'mesh-prod' },
    { ...control.nodes[0], nodeId: 'beta', origin: 'https://beta.example', meshId: 'mesh-prod' },
  ]
  const loadControl = vi.fn(async () => control)

  render(<I18nProvider><MemoryRouter initialEntries={['/slots']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={[alpha, beta]} loadFleetControl={loadControl} />} />
  </Routes></MemoryRouter></I18nProvider>)

  const filter = await screen.findByRole('combobox', { name: 'Filter' })
  expect(within(filter).getByRole('option', { name: 'Production' })).toHaveValue('mesh:mesh-prod')
  expect(within(filter).queryByRole('option', { name: 'Alpha' })).not.toBeInTheDocument()
  expect(within(filter).queryByRole('option', { name: 'Beta' })).not.toBeInTheDocument()
})

test('projected NATO identity is shown without a locally stored Access code and logical id stays technical-only', () => {
  const item = slot()
  item.access = { publicName: 'Alpha', accessGeneration: 3, status: 'active' }
  renderSlots([instance('live', item)])
  expect(screen.getByRole('link', { name: /Alpha.*Alpha Slot/ })).toBeInTheDocument()
  expect(screen.queryByText('la_alpha')).not.toBeInTheDocument()
})

test('Details → Back restores the selected global filter and exact scroll position', async () => {
  const user = userEvent.setup()
  const item = slot(); item.access = { publicName: 'Alpha', accessGeneration: 1, status: 'active' }
  const alpha = instance('live', item)
  const loadControl = vi.fn(async () => standaloneControl('alpha'))
  Object.defineProperty(window, 'scrollY', { configurable: true, value: 317 })
  const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(0); return 1 })
  function BackButton() {
    const location = useLocation()
    return <Link to={'/slots' + location.search}>Back</Link>
  }

  render(<I18nProvider><MemoryRouter initialEntries={['/slots']}><Routes>
    <Route path="/slots" element={<ServerSlots instances={[alpha]} loadFleetControl={loadControl} />} />
    <Route path="/slots/:logicalAgentId" element={<><BackButton /><ServerSlots instances={[alpha]} loadFleetControl={loadControl} /></>} />
  </Routes></MemoryRouter></I18nProvider>)

  const filter = await screen.findByRole('combobox', { name: 'Filter' })
  await user.selectOptions(filter, 'server:alpha')
  await user.click(await screen.findByRole('link', { name: /Alpha.*Alpha Slot/ }))
  expect(sessionStorage.getItem('slots-list-filter')).toBe('server:alpha')
  expect(sessionStorage.getItem('slots-scroll:server:alpha')).toBe('317')
  scrollTo.mockClear()

  await user.click(screen.getByRole('link', { name: 'Back' }))
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Filter' })).toHaveValue('server:alpha'))
  await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 317 }))
})

test('missing readable identity falls back to Unknown and logical agent ID appears only in technical details', async () => {
  const user = userEvent.setup()
  const item = slot()
  item.displayName = item.logicalAgentId
  item.access = undefined
  renderSlots([instance('live', item)])

  expect(screen.getByRole('link', { name: 'Unknown' })).toBeInTheDocument()
  expect(screen.queryByText('la_alpha')).not.toBeInTheDocument()
  await user.click(screen.getByRole('link', { name: 'Unknown' }))
  const technicalId = screen.getByText('la_alpha')
  expect(technicalId).not.toBeVisible()
  await user.click(screen.getByText('Technical details'))
  expect(technicalId).toBeVisible()
  expect(screen.getByText('Logical agent ID')).toBeInTheDocument()
})

test('successful create becomes a card from confirmed authoritative state without manual refresh', async () => {
  const user = userEvent.setup()
  vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
  const initial = instance('live', slot())
  function Harness() {
    const [fleet, setFleet] = useState<FleetInstanceView[]>([initial])
    const mutate: PersistentMutator = async (_instanceId, path, body) => {
      if (path !== '/actions/persistent/slots/create') return { ok: true, payload: { ok: true } }
      const displayName = String(body.display_name ?? '')
      const created: PersistentSlotReadModel = {
        ...slot(),
        logicalAgentId: 'la_new',
        displayName,
        authorityNodeId: 'secondary',
        slotRevision: 1,
        createdAt: '2026-10-03T10:00:00Z',
        updatedAt: '2026-10-03T10:00:00Z',
        access: { publicName: 'Bravo', accessGeneration: 1, status: 'active' },
      }
      setFleet((current) => current.map((candidate) => ({
        ...candidate,
        runtime: {
          ...candidate.runtime,
          realtime: candidate.runtime.realtime ? {
            ...candidate.runtime.realtime,
            snapshot: candidate.runtime.realtime.snapshot ? {
              ...candidate.runtime.realtime.snapshot,
              persistent: candidate.runtime.realtime.snapshot.persistent ? {
                ...candidate.runtime.realtime.snapshot.persistent,
                slots: [...candidate.runtime.realtime.snapshot.persistent.slots, created],
              } : candidate.runtime.realtime.snapshot.persistent,
            } : candidate.runtime.realtime.snapshot,
          } : candidate.runtime.realtime,
        },
      })))
      return {
        ok: true,
        payload: {
          ok: true,
          slot: { logical_agent_id: 'la_new' },
          access: { public_name: 'Bravo', access_generation: 1, access_code: '4821' },
        },
      }
    }
    return <ServerSlots instances={fleet} mutatePersistent={mutate} />
  }

  render(<I18nProvider><MemoryRouter initialEntries={['/servers/alpha/slots']}><Routes>
    <Route path="/servers/:instanceId/slots" element={<Harness />} />
  </Routes></MemoryRouter></I18nProvider>)

  await user.type(screen.getByRole('textbox', { name: 'Slot name' }), 'Build agent')
  await user.click(screen.getByRole('button', { name: 'Create slot' }))
  const createdLink = await screen.findByRole('link', { name: /Bravo.*Build agent/ })
  const createdCard = createdLink.closest('.slot-card') as HTMLElement
  expect(createdCard).toBeInTheDocument()
  expect(within(createdCard).getByText('4821')).toBeInTheDocument()
  expect(screen.getAllByText('4821').length).toBeGreaterThanOrEqual(2)
})



test('Save and Reset share one explicit horizontal policy action row', () => {
  renderSlots([instance('live', slot())])
  const save = screen.getByRole('button', { name: 'Save' })
  const reset = screen.getByRole('button', { name: 'Reset' })
  expect(save.parentElement).toBe(reset.parentElement)
  expect(save.parentElement).toHaveClass('policy-actions')
})
