import { cleanup,render,screen,waitFor,within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'
import { nativeDefaults,nativeSlot } from '../slots/testFixtures'

import type { AccessMeshSlotReadModel,ManagedFleetControlReadModel } from '../api/models'
import { fixtureFleetModel } from '../fixtures/fleet'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { I18nProvider } from '../i18n/I18nProvider'
import { Overview } from './Overview'

const SERVER_NOW = '2026-10-03T12:00:00Z'
const ACTIVE_UNTIL = '2026-10-03T12:10:00Z'

const nativeRows = new Map<string, AccessMeshSlotReadModel[]>()
function slot(id: string, name: string, state = 'active', hardExpiresAt = ACTIVE_UNTIL): AccessMeshSlotReadModel {
  return { ...nativeSlot(), slotId: 'as_' + id, publicName: name, revision: 4, codeGeneration: 1,
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
  const loadNative = vi.fn(async (id: string) => structuredClone(nativeRows.get(id) ?? []))
  const loadDefaults = vi.fn(async () => nativeDefaults())
  const mutateNative = vi.fn(async () => ({ ok: true, payload: {} }))
  const view = render(<I18nProvider><MemoryRouter><Overview model={model} instances={instances} loadFleetControl={loader} loadAccessMeshSlots={loadNative} loadAccessMeshDefaults={loadDefaults} mutateAccessMesh={mutateNative} /></MemoryRouter></I18nProvider>)
  return { ...view, loader, mutateNative, loadNative, loadDefaults }
}

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); nativeRows.clear(); vi.useRealTimers(); vi.restoreAllMocks() })

test.each([true, false])('groups Servers by observed membership when participant applied=%s', async (applied) => {
  const authority = managedControl()
  if (!applied) authority.nodes[1].appliedTopologyRevision = 0
  const controls = { 'server-a': authority,
    'server-b': applied ? { ...managedControl(), nodeId: 'node-2' } : standaloneControl('server-b'),
    'server-c': standaloneControl('server-c') }
  renderOverview(fixtureFleetModel, [], controls)
  await waitFor(() => expect(screen.getAllByText('Production').length).toBeGreaterThan(0))
  const groups = document.querySelectorAll('.fleet-server-group')
  expect(groups).toHaveLength(2)
  expect(within(groups[0] as HTMLElement).getByText('Production')).toBeInTheDocument()
  expect(within(groups[0] as HTMLElement).getByRole('article', { name: 'Server A server' })).toBeInTheDocument()
  expect(within(groups[applied ? 0 : 1] as HTMLElement).getByRole('article', { name: 'Server B server' })).toBeInTheDocument()
  expect(within(groups[1] as HTMLElement).getByText('Standalone')).toBeInTheDocument()
  expect(within(groups[1] as HTMLElement).getByRole('article', { name: 'Server C server' })).toBeInTheDocument()
  const meshLink = screen.getByRole('link', { name: new RegExp(`Production.*${applied ? 2 : 1} servers`) })
  expect(meshLink).toHaveAttribute('href', '/meshes/mesh-prod')
})

test('Agent sessions displays only slots with an active server timer', async () => {
  const active = slot('la-mesh', 'Mesh Slot')
  const expired = slot('la-expired', 'Expired Slot', 'active', '2026-10-03T11:59:59Z')
  const controls = { 'server-a': managedControl() }

  renderOverview(fixtureFleetModel, [instance('server-a', [active, expired])], controls)

  const sessions = screen.getByRole('heading', { name: 'Agent sessions' }).closest('section')!
  expect(await within(sessions).findByText('Mesh Slot')).toBeInTheDocument()
  expect(within(sessions).queryByText('Expired Slot')).not.toBeInTheDocument()
  expect(within(sessions).getAllByRole('article')).toHaveLength(1)
})
