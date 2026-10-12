import { nativeSlot, nativeDefaults } from '../slots/testFixtures'
import { nativeCodeScope } from '../slots/nativeCodeReceipt'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import type { AccessMeshMutationRequest, AccessMeshSlotReadModel, ManagedFleetControlReadModel } from '../api/models'
import { saveAccessCode } from '../access/codeVault'
import { fixtureFleetModel } from '../fixtures/fleet'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { Overview } from './Overview'

const SERVER_NOW = '2026-10-03T12:00:00Z'
const ACTIVE_UNTIL = '2026-10-03T12:10:00Z'

const nativeRows = new Map<string, AccessMeshSlotReadModel[]>()
function slot(id: string, name: string, state = 'active', hardExpiresAt = ACTIVE_UNTIL): AccessMeshSlotReadModel {
  return { ...nativeSlot(), slotId: 'as_' + id, logicalAgentId: id, publicName: name, revision: 4, codeGeneration: 1,
    issuerNodeId: id === 'la-standalone' ? 'server-c' : 'node-a',
    sessionLifecycle: { state, startedAt: '2026-10-03T11:50:00Z', hardExpiresAt, rearmAt: null,
      remainingSeconds: Math.max(0, (Date.parse(hardExpiresAt) - Date.parse(SERVER_NOW)) / 1000) } }
}

function instance(serverId: string, slots: AccessMeshSlotReadModel[]): FleetInstanceView {
  const base = fixtureFleetModel.servers.find((item) => item.instanceId === serverId)!
  nativeRows.set(serverId, slots)
  return {
    profile: { instanceId: serverId, origin: base.origin, displayName: base.displayName, credentialRef: `cred-${serverId}`, metadata: { deviceId: `d-${serverId}`, clientId: `c-${serverId}`, deviceLabel: base.displayName, scope: 'terminal:read terminal:execute', pairedAt: 1 }, createdAt: 1, updatedAt: 1 },
    runtime: {
      instanceId: serverId, status: 'live', statusSince: SERVER_NOW, authStatus: 'connected', reconnectAttempt: 0,
      realtime: {
        status: 'live', statusSince: SERVER_NOW, freshnessSince: SERVER_NOW, snapshot: {
          highWaterSeq: 1, replayFromSeq: 1, duplicateEventsPossible: false,
          instance: { application: 'terminal-mcp', version: '0.10.1', publicBaseUrl: base.origin, healthy: true, health: {}, resources: { status: 'unavailable', cpu: { status: 'unavailable' }, memory: { status: 'unavailable' }, filesystem: { status: 'unavailable' }, uptime: { status: 'unavailable' } } },
          agents: [], tasks: [], contexts: [], communications: [],
        },
        cursor: 1, highWaterSeq: 1, socketConnected: true, reconnectAttempt: 0, freshness: 'fresh', catchingUpScopes: [],
      },
    },
  }
}

function managedControl(memberIds = ['server-a', 'server-b']): ManagedFleetControlReadModel {
  return {
    schemaVersion: 3, fleetId: 'fleet-1', nodeId: 'node-a', controlNodeId: 'node-a', managed: true,
    mesh: { meshId: 'mesh-prod', displayName: 'Production', adopted: true, adoptedAt: '2026-10-03T10:00:00Z', updatedAt: SERVER_NOW },
    meshes: [{ meshId: 'mesh-prod', displayName: 'Production', adopted: true, adoptedAt: '2026-10-03T10:00:00Z', updatedAt: SERVER_NOW }],
    nodes: memberIds.map((instanceId, index) => ({ nodeId: index === 0 ? 'node-a' : `node-${index + 1}`, origin: fixtureFleetModel.servers.find((item) => item.instanceId === instanceId)?.origin, meshId: 'mesh-prod', state: 'active' as const, desiredTopologyRevision: memberIds.length, appliedTopologyRevision: memberIds.length, desiredTrustRevision: 1, appliedTrustRevision: 1, desiredPolicyRevision: 1, appliedPolicyRevision: 1, updatedAt: SERVER_NOW })),
    revisions: { routing: 1, topology: memberIds.length, trust: 1, accessPolicy: 1 }, updatedAt: SERVER_NOW,
  }
}

function standaloneControl(serverId: string): ManagedFleetControlReadModel {
  return { ...managedControl([]), nodeId: serverId, controlNodeId: serverId, managed: false, mesh: undefined, meshes: [], nodes: [], revisions: { routing: 1, topology: 1, trust: 1, accessPolicy: 1 } }
}

function renderOverview(model: FleetReadModel, instances: FleetInstanceView[], controls: Record<string, ManagedFleetControlReadModel>) {
  const loader = vi.fn(async (instanceId: string) => controls[instanceId] ?? standaloneControl(instanceId))
  const mutatePersistent = vi.fn(async () => ({ ok: true, payload: {} }))
  const loadNative = vi.fn(async (id: string) => structuredClone(nativeRows.get(id) ?? []))
  const loadDefaults = vi.fn(async () => nativeDefaults())
  const mutateNative = vi.fn(async (id: string, request: AccessMeshMutationRequest) => {
    if (!('slot_id' in request)) throw new Error('unexpected test action')
    const row = nativeRows.get(id)?.find((item) => item.slotId === request.slot_id)
    if (!row || request.expected_revision !== row.revision) return { ok: false, code: 'access_mesh_revision_conflict', payload: { outcome: 'not_committed' } }
    row.revision++
    if (request.action === 'delete') { row.state = 'deleted'; row.sessionLifecycle.state = 'ended' }
    if (request.action === 'suspend') { row.state = 'suspended'; row.sessionLifecycle.state = 'suspended' }
    return { ok: true, payload: { revision: row.revision } }
  })
  const view = render(<I18nProvider><MemoryRouter><Overview model={model} instances={instances} loadFleetControl={loader} mutatePersistent={mutatePersistent} loadAccessMeshSlots={loadNative} loadAccessMeshDefaults={loadDefaults} mutateAccessMesh={mutateNative} /></MemoryRouter></I18nProvider>)
  return { ...view, loader, mutatePersistent, mutateNative, loadNative, loadDefaults }
}

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); nativeRows.clear(); vi.useRealTimers(); vi.restoreAllMocks() })

test('groups Servers by Mesh with standalone last and renders authoritative Mesh index count/link', async () => {
  const controls = { 'server-a': managedControl(), 'server-b': managedControl(), 'server-c': standaloneControl('server-c') }
  renderOverview(fixtureFleetModel, [], controls)
  await waitFor(() => expect(screen.getAllByText('Production').length).toBeGreaterThan(0))
  const groups = document.querySelectorAll('.fleet-server-group')
  expect(groups).toHaveLength(2)
  expect(within(groups[0] as HTMLElement).getByText('Production')).toBeInTheDocument()
  expect(within(groups[0] as HTMLElement).getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(within(groups[0] as HTMLElement).getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(within(groups[1] as HTMLElement).getByText('Standalone')).toBeInTheDocument()
  expect(within(groups[1] as HTMLElement).getByRole('article', { name: 'Server C server' })).toBeInTheDocument()
  const meshLink = screen.getByRole('link', { name: /Production.*2 servers/ })
  expect(meshLink).toHaveAttribute('href', '/meshes/mesh-prod')
})

test('shows only active-timer Agent Sessions, grouped by Mesh then standalone, with code/actions and correct Details route', async () => {
  saveAccessCode('la-mesh', { code: '4821', generation: 1, publicName: 'Mesh Slot' }, localStorage, nativeCodeScope('node-a', 'as_la-mesh'))
  saveAccessCode('la-standalone', { code: '7314', generation: 1, publicName: 'Solo Slot' }, localStorage, nativeCodeScope('server-c', 'as_la-standalone'))
  const expired = slot('la-expired', 'Expired Slot', 'active', '2026-10-03T11:59:59Z')
  const mesh = slot('la-mesh', 'Mesh Slot')
  const standalone = slot('la-standalone', 'Solo Slot')
  const controls = { 'server-a': managedControl(), 'server-b': managedControl(), 'server-c': standaloneControl('server-c') }
  const { mutatePersistent, mutateNative } = renderOverview(fixtureFleetModel, [instance('server-a', [mesh, expired]), instance('server-c', [standalone])], controls)
  await waitFor(() => expect(screen.getAllByText('Production').length).toBeGreaterThan(0))
  const sessionSection = screen.getByRole('heading', { name: 'Agent sessions' }).closest('section')!
  expect(await within(sessionSection).findByText('Mesh Slot')).toBeInTheDocument()
  expect(within(sessionSection).getByText('Solo Slot')).toBeInTheDocument()
  expect(within(sessionSection).queryByText('Expired Slot')).not.toBeInTheDocument()
  expect(within(sessionSection).getByText('4821')).toBeInTheDocument()
  expect(within(sessionSection).getAllByText('Session active')).toHaveLength(2)
  expect(within(sessionSection).getAllByText(/10min 00s|9min 59s/).length).toBeGreaterThan(0)
  const groups = sessionSection.querySelectorAll('.fleet-session-group')
  expect(within(groups[0] as HTMLElement).getByText('Production')).toBeInTheDocument()
  expect(within(groups[1] as HTMLElement).getByText('Server C')).toBeInTheDocument()
  const meshCard = within(sessionSection).getByText('Mesh Slot').closest('article')!
  expect(within(meshCard).getByRole('button', { name: 'Copy Access code — Mesh Slot' })).toBeInTheDocument()
  expect(within(meshCard).getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  expect(meshCard.querySelector('summary[aria-label="More actions"]')).toBeInTheDocument()
  expect(within(meshCard).getByRole('button', { name: 'Rotate Access code' })).toBeInTheDocument()
  const deleteButton = within(meshCard).getByRole('button', { name: 'Delete' })
  expect(deleteButton).toBeInTheDocument()
  expect(within(meshCard).getByRole('link', { name: 'Details' })).toHaveAttribute('href', expect.stringMatching(/^\/meshes\/mesh-prod\/persistent\/la-mesh\?/))
  await userEvent.click(deleteButton)
  let deleteDialog = screen.getByRole('dialog', { name: 'Delete' })
  expect(within(deleteDialog).getByText('Mesh Slot')).toBeInTheDocument()
  await userEvent.click(within(deleteDialog).getByRole('button', { name: 'Cancel' }))
  expect(mutatePersistent).not.toHaveBeenCalled()
  expect(mutateNative).not.toHaveBeenCalled()
  await userEvent.click(deleteButton)
  deleteDialog = screen.getByRole('dialog', { name: 'Delete' })
  await userEvent.click(within(deleteDialog).getByRole('button', { name: 'Delete' }))
  await waitFor(() => expect(mutateNative).toHaveBeenCalledWith('server-a', expect.objectContaining({ action: 'delete', slot_id: 'as_la-mesh', expected_revision: 4 })))
  await waitFor(() => expect(screen.queryByText('Mesh Slot')).not.toBeInTheDocument())
  const soloCard = screen.getByText('Solo Slot').closest('article')!
  await userEvent.click(within(soloCard).getByRole('button', { name: 'Pause' }))
  await waitFor(() => expect(mutateNative).toHaveBeenCalledWith('server-c', expect.objectContaining({ action: 'suspend', slot_id: 'as_la-standalone', expected_revision: 4 })))
  expect(mutatePersistent).not.toHaveBeenCalled()
})

test('reactively updates membership, Mesh count and removes a session when its authoritative timer has ended', async () => {
  const meshSlot = slot('la-mesh', 'Mesh Slot')
  const controls1: Record<string, ManagedFleetControlReadModel> = { 'server-a': managedControl(['server-a']), 'server-b': standaloneControl('server-b'), 'server-c': standaloneControl('server-c') }
  const loader = vi.fn(async (instanceId: string) => controls1[instanceId] ?? standaloneControl(instanceId))
  const loadNative = vi.fn(async (id: string) => structuredClone(nativeRows.get(id) ?? []))
  const loadDefaults = vi.fn(async () => nativeDefaults())
  const { rerender } = render(<I18nProvider><MemoryRouter><Overview model={fixtureFleetModel} instances={[instance('server-a', [meshSlot])]} loadFleetControl={loader} loadAccessMeshSlots={loadNative} loadAccessMeshDefaults={loadDefaults} /></MemoryRouter></I18nProvider>)
  await waitFor(() => expect(screen.getByRole('link', { name: /Production.*1 servers/ })).toBeInTheDocument())
  expect(await screen.findByText('Mesh Slot')).toBeInTheDocument()

  const controls2: Record<string, ManagedFleetControlReadModel> = { 'server-a': managedControl(['server-a', 'server-b']), 'server-b': managedControl(['server-a', 'server-b']), 'server-c': standaloneControl('server-c') }
  loader.mockImplementation(async (instanceId: string) => controls2[instanceId] ?? standaloneControl(instanceId))
  const expiredSnapshot = instance('server-a', [{ ...meshSlot, sessionLifecycle: { ...meshSlot.sessionLifecycle, remainingSeconds: 0 } }])
  const changedModel = { ...fixtureFleetModel, servers: fixtureFleetModel.servers.map((server) => ({ ...server })) }
  rerender(<I18nProvider><MemoryRouter><Overview key="refreshed-native-read" model={changedModel} instances={[expiredSnapshot]} loadFleetControl={loader} loadAccessMeshSlots={loadNative} loadAccessMeshDefaults={loadDefaults} /></MemoryRouter></I18nProvider>)
  await waitFor(() => expect(screen.getByRole('link', { name: /Production.*2 servers/ })).toBeInTheDocument())
  expect(screen.queryByText('Mesh Slot')).not.toBeInTheDocument()
  const productionGroup = [...document.querySelectorAll('.fleet-server-group')].find((element) => element.textContent?.includes('Production')) as HTMLElement
  expect(within(productionGroup).getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
})

test('mobile filter keeps All / Problems / Live in one non-wrapping three-column control', () => {
  renderOverview(fixtureFleetModel, [], {})
  const control = screen.getByRole('group', { name: 'Filter servers' })
  expect(within(control).getAllByRole('button')).toHaveLength(3)
  expect(control.parentElement).toHaveAttribute('data-mobile-layout', 'single-line-three-segment')
  expect([...within(control).getAllByRole('button')].map((button) => button.textContent)).toEqual(['All (3)', 'Problems (2)', 'Live (1)'])
})
