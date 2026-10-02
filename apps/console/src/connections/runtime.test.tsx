import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { PairingTransport } from '../auth/transport'
import type { StoredConnection } from '../auth/types'
import type { ManagedFleetControlReadModel } from '../api/models'
import type { KeyValueStorage } from '../auth/vault'
import { I18nProvider } from '../i18n/I18nProvider'
import { Connections } from '../routes/Connections'
import { BrowserConnectionRegistry } from './registry'
import { saveCachedFleetControl } from './controlState'
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

test('restores every stored profile into app runtime and removes only the selected profile', async () => {
  const storage = new MemoryStorage()
  let nextId = 0
  const registry = new BrowserConnectionRegistry(
    storage,
    () => 10_000,
    () => ['alpha', 'beta'][nextId++] ?? 'fallback',
  )
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')
  registry.add(connection('https://beta.example', 'beta'), 'Beta')

  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const origin = new URL(String(input)).origin
    const suffix = origin.includes('alpha') ? 'alpha-next' : 'beta-next'
    return new Response(
      JSON.stringify({
        access_token: 'access-' + suffix,
        token_type: 'Bearer',
        expires_in: 120,
        refresh_token: 'refresh-' + suffix,
        scope: 'terminal:read',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  })

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider registry={registry} transport={new PairingTransport(fetcher)}>
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  expect(screen.getByRole('heading', { name: 'Connections' })).toBeInTheDocument()
  expect(screen.getByText('Alpha')).toBeInTheDocument()
  expect(screen.getByText('Beta')).toBeInTheDocument()
  await waitFor(() => expect(screen.getAllByText('Connected')).toHaveLength(2))
  expect(fetcher).toHaveBeenCalledTimes(2)

  const removeButtons = screen.getAllByRole('button', { name: 'Remove' })
  await userEvent.click(removeButtons[0])

  await waitFor(() => expect(registry.list().map((profile) => profile.displayName)).toEqual(['Beta']))
  expect(screen.getByText('1 saved')).toBeInTheDocument()
  expect(screen.getAllByText('Connected')).toHaveLength(1)
  expect(registry.credential('beta')?.refreshToken).toBe('refresh-beta-next')
})

afterEach(() => { cleanup(); localStorage.clear() })

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



test('cold start preserves last authoritative Production membership while control observation is unavailable', async () => {
  const storage = new MemoryStorage()
  let nextId = 0
  const ids = ['main-profile', 'bacloud-profile', 'firstbyte-profile', 'tokyo-profile']
  const registry = new BrowserConnectionRegistry(storage, () => 10_000, () => ids[nextId++] ?? 'fallback')
  registry.add(connection('https://main.example', 'main'), 'Main')
  registry.add(connection('https://bacloud.example', 'bacloud'), 'BacLOUD')
  registry.add(connection('https://firstbyte.example', 'firstbyte'), 'Firstbyte')
  registry.add(connection('https://tokyo.example', 'tokyo'), 'Tokyo')

  const control: ManagedFleetControlReadModel = {
    schemaVersion: 3,
    fleetId: 'fleet-prod',
    nodeId: 'main',
    controlNodeId: 'main',
    managed: true,
    mesh: { meshId: 'mesh-prod', displayName: 'Production', adopted: true, updatedAt: '2026-10-02T06:00:00Z' },
    meshes: [{ meshId: 'mesh-prod', displayName: 'Production', adopted: true, updatedAt: '2026-10-02T06:00:00Z' }],
    nodes: [
      ['main', 'https://main.example'],
      ['bacloud', 'https://bacloud.example'],
      ['firstbyte', 'https://firstbyte.example'],
      ['tokyo', 'https://tokyo.example'],
    ].map(([nodeId, origin]) => ({
      nodeId,
      origin,
      meshId: 'mesh-prod',
      state: 'active' as const,
      desiredTopologyRevision: 8,
      appliedTopologyRevision: 8,
      desiredTrustRevision: 5,
      appliedTrustRevision: 5,
      desiredPolicyRevision: 4,
      appliedPolicyRevision: 4,
      updatedAt: '2026-10-02T06:00:00Z',
    })),
    policy: {
      durationSeconds: 1380,
      warningAfterSeconds: 1200,
      alertAfterSeconds: 1320,
      rearmAfterSeconds: 180,
      legacyAdmissionEnabled: false,
      revision: 4,
      updatedAt: '2026-10-02T06:00:00Z',
    },
    revisions: { routing: 3, topology: 8, trust: 5, accessPolicy: 4 },
    updatedAt: '2026-10-02T06:00:00Z',
  }
  saveCachedFleetControl('main-profile', control, 1_000)

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider
        registry={registry}
        transport={new PairingTransport(vi.fn())}
        restoreOnMount={false}
      >
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  for (const name of ['Main', 'BacLOUD', 'Firstbyte', 'Tokyo']) {
    const card = screen.getByRole('heading', { name }).closest('article')!
    expect(within(card).getByText('Mesh: Production · Stale')).toBeInTheDocument()
  }
  expect(screen.queryByRole('heading', { name: 'Standalone' })).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Unknown' })).not.toBeInTheDocument()
})

test('missing control observation is Unknown rather than Standalone', () => {
  const storage = new MemoryStorage()
  const registry = new BrowserConnectionRegistry(storage, () => 10_000, () => 'alpha')
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider
        registry={registry}
        transport={new PairingTransport(vi.fn())}
        restoreOnMount={false}
      >
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  const card = screen.getByRole('heading', { name: 'Alpha' }).closest('article')!
  expect(within(card).getByText('Mesh: Unknown')).toBeInTheDocument()
  expect(screen.getAllByRole('heading', { name: 'Unknown' }).length).toBeGreaterThan(0)
  expect(screen.queryByRole('heading', { name: 'Standalone' })).not.toBeInTheDocument()
})

test('managed mesh membership supports standalone attach move and detach with explicit writes', async () => {
  const storage = new MemoryStorage()
  let nextId = 0
  const registry = new BrowserConnectionRegistry(
    storage,
    () => 10_000,
    () => ['alpha', 'beta'][nextId++] ?? 'fallback',
  )
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')
  registry.add(connection('https://beta.example', 'beta'), 'Beta')

  let topologyRevision = 2
  let trustRevision = 2
  const meshes = [
    {
      mesh_id: 'mesh-a',
      display_name: 'Production',
      adopted: true,
      adopted_at: '2026-10-02T05:00:00Z',
      updated_at: '2026-10-02T06:00:00Z',
    },
    {
      mesh_id: 'mesh-b',
      display_name: 'Staging',
      adopted: true,
      adopted_at: '2026-10-02T05:30:00Z',
      updated_at: '2026-10-02T06:00:00Z',
    },
  ]
  let nodes = ['alpha', 'beta'].map((nodeId) => ({
    node_id: nodeId,
    origin: 'https://' + nodeId + '.example',
    mesh_id: null as string | null,
    state: 'active',
    desired_topology_revision: 2,
    applied_topology_revision: 2,
    desired_trust_revision: 2,
    applied_trust_revision: 2,
    desired_policy_revision: 4,
    applied_policy_revision: 4,
    updated_at: '2026-10-02T06:00:00Z',
  }))

  const controlEnvelope = (nodeId: string) => {
    const local = nodes.find((node) => node.node_id === nodeId)
    const localMesh = local?.mesh_id
      ? meshes.find((mesh) => mesh.mesh_id === local.mesh_id)
      : undefined
    return {
      ok: true,
      control: {
        schema_version: 3,
        fleet_id: 'fleet-a',
        node_id: nodeId,
        control_node_id: 'alpha',
        managed: true,
        mesh: localMesh ?? null,
        meshes,
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
        revisions: {
          routing: 1,
          topology: topologyRevision,
          trust: trustRevision,
          access_policy: 4,
        },
        updated_at: '2026-10-02T06:00:00Z',
      },
    }
  }

  const bumpMembership = (nodeId: string, meshId: string | null) => {
    topologyRevision += 1
    trustRevision += 1
    nodes = nodes.map((node) => (
      node.node_id === nodeId
        ? {
            ...node,
            mesh_id: meshId,
            desired_topology_revision: topologyRevision,
            applied_topology_revision: topologyRevision,
            desired_trust_revision: trustRevision,
            applied_trust_revision: trustRevision,
          }
        : {
            ...node,
            desired_topology_revision: topologyRevision,
            applied_topology_revision: topologyRevision,
            desired_trust_revision: trustRevision,
            applied_trust_revision: trustRevision,
          }
    ))
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
    if (url.pathname === '/actions/fleet/control/enrollment' && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify({
        ok: true,
        enrollment: {
          node_id: nodeId,
          origin: 'https://' + nodeId + '.example',
          public_key: 'public-' + nodeId,
          auth_token: 'ingress-' + nodeId,
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.pathname === '/actions/fleet/control' && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify(controlEnvelope(nodeId)), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.pathname === '/actions/fleet/control/nodes/upsert' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      expect(url.origin).toBe('https://alpha.example')
      expect(body).toEqual({
        node_id: 'beta',
        mesh_id: 'mesh-a',
        origin: 'https://beta.example',
        public_key: 'public-beta',
        auth_token: 'ingress-beta',
        expected_topology_revision: 2,
      })
      bumpMembership('beta', 'mesh-a')
      return new Response(JSON.stringify(controlEnvelope('alpha')), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.pathname === '/actions/fleet/control/nodes/move' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      expect(body).toEqual({
        node_id: 'beta',
        target_mesh_id: 'mesh-b',
        expected_topology_revision: 3,
      })
      bumpMembership('beta', 'mesh-b')
      return new Response(JSON.stringify(controlEnvelope('alpha')), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.pathname === '/actions/fleet/control/nodes/detach' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      expect(body).toEqual({
        node_id: 'beta',
        expected_topology_revision: 4,
      })
      bumpMembership('beta', null)
      return new Response(JSON.stringify(controlEnvelope('alpha')), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.pathname === '/actions/fleet/control/trust/rotate' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      expect(url.origin).toBe('https://beta.example')
      expect(body).toEqual({ expected_trust_revision: trustRevision })
      trustRevision += 1
      nodes = nodes.map((node) => ({
        ...node,
        desired_trust_revision: trustRevision,
        applied_trust_revision: trustRevision,
      }))
      return new Response(JSON.stringify(controlEnvelope('beta')), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
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
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  const betaCard = () => screen.getByRole('heading', { name: 'Beta' }).closest('article')!
  await waitFor(() => expect(within(betaCard()).getByLabelText('Mesh membership')).toBeEnabled())
  expect(within(betaCard()).getByLabelText('Mesh membership')).toHaveValue('')
  expect(screen.getByRole('heading', { name: 'Standalone' })).toBeInTheDocument()

  await userEvent.selectOptions(within(betaCard()).getByLabelText('Mesh membership'), 'mesh-a')
  await waitFor(() => {
    expect(fetcher.mock.calls.filter(
      ([input]) => new URL(String(input)).pathname.endsWith('/nodes/upsert'),
    )).toHaveLength(1)
  })
  await waitFor(() => expect(within(betaCard()).getByLabelText('Mesh membership')).toHaveValue('mesh-a'))

  await userEvent.selectOptions(within(betaCard()).getByLabelText('Mesh membership'), 'mesh-b')
  await waitFor(() => {
    expect(fetcher.mock.calls.filter(
      ([input]) => new URL(String(input)).pathname.endsWith('/nodes/move'),
    )).toHaveLength(1)
  })
  await waitFor(() => expect(within(betaCard()).getByLabelText('Mesh membership')).toHaveValue('mesh-b'))

  await userEvent.selectOptions(within(betaCard()).getByLabelText('Mesh membership'), '')
  await waitFor(() => {
    expect(fetcher.mock.calls.filter(
      ([input]) => new URL(String(input)).pathname.endsWith('/nodes/detach'),
    )).toHaveLength(1)
  })
  await waitFor(() => expect(within(betaCard()).getByLabelText('Mesh membership')).toHaveValue(''))

  expect(screen.getAllByText(/Reachability:/)).toHaveLength(2)
  const rotateButtons = screen.getAllByRole('button', { name: 'Rotate trust' })
  await userEvent.click(rotateButtons[1])
  await waitFor(() => {
    expect(fetcher.mock.calls.filter(
      ([input]) => new URL(String(input)).pathname.endsWith('/trust/rotate'),
    )).toHaveLength(1)
  })
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
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  const betaCard = () => screen.getByRole('heading', { name: 'Beta' }).closest('article')!
  const membership = () => within(betaCard()).getByLabelText('Mesh membership')
  await waitFor(() => expect(membership()).toBeEnabled())
  expect(membership()).toHaveValue('')

  await userEvent.selectOptions(membership(), 'mesh-a')
  await waitFor(() => expect(membership()).toHaveValue('mesh-a'))
  expect(within(betaCard()).getByText('Pending')).toBeInTheDocument()

  rejectUpsert?.(new Response(JSON.stringify({
    ok: false,
    code: 'revision_conflict',
    error: 'revision_conflict',
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

  await waitFor(() => expect(membership()).toHaveValue(''))
  expect(within(betaCard()).getByText('Failed: revision_conflict')).toBeInTheDocument()
  expect(within(betaCard()).getByText('Mesh: Standalone')).toBeInTheDocument()
})
