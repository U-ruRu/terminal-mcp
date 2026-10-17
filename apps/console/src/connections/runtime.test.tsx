import { cleanup,render,screen,waitFor,within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter,Route,Routes } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import { PairingTransport } from '../auth/transport'
import type { StoredConnection } from '../auth/types'
import type { KeyValueStorage } from '../auth/vault'
import { I18nProvider } from '../i18n/I18nProvider'
import { Connections } from '../routes/Connections'
import { BrowserConnectionRegistry } from './registry'
import { ConnectionRuntimeProvider } from './runtime'

class MemoryStorage implements KeyValueStorage {
  data = new Map<string, string>()

  getItem(key: string) {
    return this.data.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.data.set(key, value)
  }

  removeItem(key: string) {
    this.data.delete(key)
  }
}

function connection(origin: string, suffix: string): StoredConnection {
  return {
    origin,
    deviceId: 'device-' + suffix,
    clientId: 'client-' + suffix,
    deviceLabel: 'Android console',
    scope: 'terminal:read',
    refreshToken: 'refresh-' + suffix,
    pairedAt: 1000,
  }
}

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

test('can expose stored profiles without independently rotating refresh tokens', async () => {
  const storage = new MemoryStorage()
  const registry = new BrowserConnectionRegistry(storage, () => 10_000, () => 'alpha')
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')
  const fetcher = vi.fn()

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider
        registry={registry}
        transport={new PairingTransport(fetcher)}
        restoreOnMount={false}
      >
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  expect(screen.getByText('Alpha')).toBeInTheDocument()
  expect(screen.getByText('Stored')).toBeInTheDocument()
  await Promise.resolve()
  expect(fetcher).not.toHaveBeenCalled()
  expect(registry.credential('alpha')?.refreshToken).toBe('refresh-alpha')
})

test('rejected managed membership mutation rolls the optimistic projection back and stays failed', async () => {
  const storage = new MemoryStorage()
  let nextId = 0
  const registry = new BrowserConnectionRegistry(
    storage,
    () => 10_000,
    () => ['alpha', 'beta'][nextId++] ?? 'fallback',
  )
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')
  registry.add(connection('https://beta.example', 'beta'), 'Beta')

  const nodes = [
    {
      node_id: 'alpha',
      origin: 'https://alpha.example',
      mesh_id: 'mesh-a',
      state: 'active',
      desired_topology_revision: 2,
      applied_topology_revision: 2,
      desired_trust_revision: 2,
      applied_trust_revision: 2,
      desired_policy_revision: 4,
      applied_policy_revision: 4,
      updated_at: '2026-10-02T06:00:00Z',
    },
    {
      node_id: 'beta',
      origin: 'https://beta.example',
      mesh_id: null,
      state: 'active',
      desired_topology_revision: 2,
      applied_topology_revision: 2,
      desired_trust_revision: 2,
      applied_trust_revision: 2,
      desired_policy_revision: 4,
      applied_policy_revision: 4,
      updated_at: '2026-10-02T06:00:00Z',
    },
  ]
  const mesh = {
    mesh_id: 'mesh-a',
    display_name: 'Production',
    adopted: true,
    adopted_at: '2026-10-02T05:00:00Z',
    updated_at: '2026-10-02T06:00:00Z',
  }
  const envelope = (nodeId: string) => ({
    ok: true,
    control: {
      schema_version: 3,
      fleet_id: 'fleet-a',
      node_id: nodeId,
      control_node_id: 'alpha',
      managed: true,
      mesh: nodeId === 'alpha' ? mesh : null,
      meshes: [mesh],
      nodes,
      policy: {
        duration_seconds: 1380,
        warning_after_seconds: 1200,
        alert_after_seconds: 1320,
        rearm_after_seconds: 180,
        legacy_admission_enabled: false,
        revision: 4,
        updated_at: '2026-10-02T06:00:00Z',
      },
      revisions: { routing: 1, topology: 2, trust: 2, access_policy: 4 },
      updated_at: '2026-10-02T06:00:00Z',
    },
  })

  let rejectUpsert: ((response: Response) => void) | undefined
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input))
    const nodeId = url.origin.includes('alpha') ? 'alpha' : 'beta'
    if (url.pathname.includes('oauth') || url.pathname.includes('token')) {
      return new Response(JSON.stringify({
        access_token: 'access-' + nodeId,
        token_type: 'Bearer',
        expires_in: 120,
        refresh_token: 'refresh-' + nodeId + '-next',
        scope: 'terminal:read terminal:execute',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control' && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify(envelope(nodeId)), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control/enrollment' && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify({
        ok: true,
        enrollment: {
          node_id: nodeId,
          origin: `https://${nodeId}.example`,
          public_key: 'public-' + nodeId,
          auth_token: 'ingress-' + nodeId,
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control/nodes/upsert' && init?.method === 'POST') {
      return await new Promise<Response>((resolve) => { rejectUpsert = resolve })
    }
    return new Response(JSON.stringify({ error: 'unexpected_request' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetcher)

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider registry={registry} transport={new PairingTransport(fetcher)}>
        <MemoryRouter initialEntries={['/meshes/mesh-a']}>
          <Routes><Route path="/meshes/:meshId" element={<Connections />} /></Routes>
        </MemoryRouter>
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  const betaCard = () => screen.getByRole('heading', { name: 'Beta' }).closest('article')!
  const target = () => within(betaCard()).getByRole('group', { name: 'Target state' })
  const apply = () => within(betaCard()).getByRole('button', { name: 'Apply membership' })
  await waitFor(() => expect(apply()).toBeEnabled())

  await userEvent.click(within(target()).getByRole('button', { name: 'Production' }))
  await userEvent.click(apply())
  await waitFor(() => expect(within(betaCard()).getByText('Applying')).toBeInTheDocument())

  rejectUpsert?.(new Response(JSON.stringify({
    ok: false,
    code: 'revision_conflict',
    error: 'revision_conflict',
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

  await waitFor(() => expect(apply()).toBeEnabled())
  expect(within(betaCard()).getByText(/Attention required.*revision_conflict/)).toBeInTheDocument()
  expect(within(betaCard()).getByText(/revision_conflict/)).toBeInTheDocument()
  expect(within(betaCard()).getByText('Mesh: Standalone')).toBeInTheDocument()
  expect(within(betaCard()).getByText('Authoritative state: Standalone')).toBeInTheDocument()
})

test('creates a second mesh on an independently selected standalone control node', async () => {
  const storage = new MemoryStorage()
  let nextId = 0
  const registry = new BrowserConnectionRegistry(
    storage,
    () => 10_000,
    () => ['alpha', 'beta'][nextId++] ?? 'fallback',
  )
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')
  registry.add(connection('https://beta.example', 'beta'), 'Beta')

  let betaManaged = false
  const policy = {
    duration_seconds: 1380,
    warning_after_seconds: 1200,
    alert_after_seconds: 1320,
    rearm_after_seconds: 180,
    legacy_admission_enabled: false,
    revision: 1,
    updated_at: '2026-10-02T06:00:00Z',
  }
  const node = (nodeId: string, meshId: string | null) => ({
    node_id: nodeId,
    origin: `https://${nodeId}.example`,
    mesh_id: meshId,
    state: 'active',
    desired_topology_revision: 1,
    applied_topology_revision: 1,
    desired_trust_revision: 1,
    applied_trust_revision: 1,
    desired_policy_revision: 1,
    applied_policy_revision: 1,
    updated_at: '2026-10-02T06:00:00Z',
  })
  const meshA = {
    mesh_id: 'mesh-a', display_name: 'Production', adopted: true,
    adopted_at: '2026-10-02T05:00:00Z', updated_at: '2026-10-02T06:00:00Z',
  }
  const meshB = {
    mesh_id: 'mesh-b', display_name: 'Staging', adopted: true,
    adopted_at: '2026-10-02T06:30:00Z', updated_at: '2026-10-02T06:30:00Z',
  }
  const envelope = (nodeId: string) => {
    if (nodeId === 'beta' && !betaManaged) {
      return { ok: true, control: {
        schema_version: 3, fleet_id: 'fleet-a', node_id: 'beta', control_node_id: 'alpha',
        managed: false, mesh: null, meshes: [], nodes: [], policy: null,
        revisions: { routing: 0, topology: 0, trust: 0, access_policy: 0 },
        updated_at: '2026-10-02T06:00:00Z',
      } }
    }
    const controlNodeId = nodeId === 'beta' ? 'beta' : 'alpha'
    const mesh = nodeId === 'beta' ? meshB : meshA
    return { ok: true, control: {
      schema_version: 3, fleet_id: 'fleet-a', node_id: nodeId, control_node_id: controlNodeId,
      managed: true, mesh: null, meshes: [mesh], nodes: [node(nodeId, mesh.mesh_id)], policy,
      revisions: { routing: 1, topology: 1, trust: 1, access_policy: 1 },
      updated_at: '2026-10-02T06:30:00Z',
    } }
  }

  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input))
    const nodeId = url.origin.includes('alpha') ? 'alpha' : 'beta'
    if (url.pathname.includes('oauth') || url.pathname.includes('token')) {
      return new Response(JSON.stringify({
        access_token: 'access-' + nodeId,
        token_type: 'Bearer',
        expires_in: 120,
        refresh_token: 'refresh-' + nodeId + '-next',
        scope: 'terminal:read terminal:execute',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control' && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify(envelope(nodeId)), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control/enrollment' && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify({ ok: true, enrollment: {
        node_id: nodeId,
        origin: `https://${nodeId}.example`,
        public_key: 'public-' + nodeId,
        auth_token: 'ingress-' + nodeId,
      } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control/adopt' && init?.method === 'POST') {
      expect(url.origin).toBe('https://beta.example')
      expect(JSON.parse(String(init.body))).toMatchObject({ display_name: 'Staging', control_node_id: 'beta' })
      expect(JSON.parse(String(init.body)).mesh_id).toMatch(/^mesh-[0-9a-f]{32}$/)
      betaManaged = true
      return new Response(JSON.stringify(envelope('beta')), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response(JSON.stringify({ error: 'unexpected_request' }), { status: 500, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetcher)

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider registry={registry} transport={new PairingTransport(fetcher)}>
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  await waitFor(() => expect(
    screen.getAllByRole('link', { name: 'Production' }).some((item) => item.getAttribute('href') === '/meshes/mesh-a'),
  ).toBe(true))
  await waitFor(() => expect(screen.getByLabelText('Control node')).toBeEnabled())
  expect(within(screen.getByLabelText('Control node')).queryByRole('option', { name: 'Alpha' })).not.toBeInTheDocument()
  expect(within(screen.getByLabelText('Control node')).getByRole('option', { name: 'Beta' })).toBeInTheDocument()
  await userEvent.selectOptions(screen.getByLabelText('Control node'), 'beta')
  await userEvent.clear(screen.getByLabelText('New mesh name'))
  await userEvent.type(screen.getByLabelText('New mesh name'), 'Staging')
  await userEvent.click(screen.getByRole('button', { name: 'Create mesh' }))

  await waitFor(() => {
    expect(fetcher.mock.calls.some(([input, init]) => (
      new URL(String(input)).pathname === '/actions/fleet/control/adopt' && init?.method === 'POST'
    ))).toBe(true)
  })
  await waitFor(() => expect(
    screen.getAllByRole('link', { name: 'Staging' }).some((item) => item.getAttribute('href') === '/meshes/mesh-b'),
  ).toBe(true))
  await waitFor(() => {
    expect(within(screen.getByLabelText('Control node')).queryByRole('option', { name: 'Beta' })).not.toBeInTheDocument()
  })
})
