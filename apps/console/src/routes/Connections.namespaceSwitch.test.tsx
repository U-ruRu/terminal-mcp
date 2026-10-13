import { cleanup, render, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import type { ManagedFleetControlReadModel } from '../api/models'
import type { useConnectionRuntime } from '../connections/runtime'
import { readMeshHandoff } from '../connections/meshHandoff'
import { fleetInstance, meshControl, standaloneControl } from '../slots/testFixtures'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { Connections } from './Connections'

const mocked = vi.hoisted(() => ({ value: null as ReturnType<typeof useConnectionRuntime> | null }))
vi.mock('../connections/runtime', () => ({ useConnectionRuntime: () => mocked.value }))
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
function view() { return render(<I18nProvider><MemoryRouter initialEntries={['/meshes/mesh-prod']}><Routes><Route path="/meshes/:meshId" element={<Connections />} /></Routes></MemoryRouter></I18nProvider>) }
function betaCard() { return [...document.querySelectorAll<HTMLElement>('article.connection-card')].find((item) => item.textContent?.includes('Beta server'))! }

function control(id: string, authority: string, meshId: string, namespace: string, members: string[]): ManagedFleetControlReadModel {
  const original = meshControl(id)
  return { ...original, namespaceTransitionSupported: true, fleetId: namespace, controlNodeId: authority,
    meshes: [{ ...original.meshes[0], meshId, displayName: meshId === 'mesh-prod' ? 'Production' : 'Destination' }],
    nodes: members.map((member) => ({ ...original.nodes[0], nodeId: member, origin: `https://${member}.example`, meshId })),
    nativeAccessTrust: { state: 'ready', ready: true, meshId, peers: {}, missingTransport: [] } }
}

test.each([[true, false], [true, true], [false, false]])('Switch confirms autonomous target before cross-namespace Join, supported=%s, interruption=%s', async (supported, interrupt) => {
  let phase = 'source'
  const writes: string[] = []
  const get = async (id: string) => {
    if (id === 'gamma') return control(id, 'gamma', 'mesh-other', 'other-namespace', phase === 'joined' ? ['gamma', 'beta'] : ['gamma'])
    if (id === 'alpha') return control(id, 'alpha', 'mesh-prod', 'source-namespace', phase === 'source' ? ['alpha', 'beta'] : ['alpha'])
    if (phase === 'source') return { ...control(id, 'alpha', 'mesh-prod', 'source-namespace', ['alpha', 'beta']), namespaceTransitionSupported: supported }
    if (phase === 'joined') return control(id, 'gamma', 'mesh-other', 'other-namespace', ['gamma', 'beta'])
    return { ...standaloneControl(id), managed: true, namespaceTransitionSupported: true }
  }
  const write = vi.fn(async (id: string, path: string) => {
    writes.push(path)
    if (path.endsWith('/detach')) {
      expect(readMeshHandoff(localStorage, 'beta')?.targetMeshId).toBe('mesh-other')
      phase = 'detached'
      return { ok: true, control: await get('alpha') }
    }
    expect(id).toBe('gamma')
    expect(phase).toBe('detached')
    phase = 'joined'
    return { ok: true, control: await get('gamma') }
  })
  let unavailableAfterDetach = interrupt
  mocked.value = { profiles: ['alpha', 'beta', 'gamma'].map((id) => fleetInstance(id).profile),
    states: Object.fromEntries(['alpha', 'beta', 'gamma'].map((id) => [id, { status: 'connected', accessToken: 'synthetic', accessExpiresAt: 9999999999999 }])),
    error: null, pair: vi.fn(), retry: vi.fn(), disconnect: vi.fn(), client: (id) => ({
      fleetControl: async () => {
        if (id === 'beta' && phase === 'detached' && unavailableAfterDetach) throw new Error('network_error')
        return get(id)
      },
      fleetControlMutation: (path) => write(id, path),
      fleetEnrollment: async () => ({ nodeId: id, origin: `https://${id}.example`, publicKey: 'synthetic', authToken: 'synthetic' }),
      meshVpnStatus: vi.fn(), meshVpnMutation: vi.fn(),
    }) }
  const first = view()
  await waitFor(() => expect(within(betaCard()).getByRole('button', { name: 'Destination' })).toBeEnabled())
  await userEvent.click(within(betaCard()).getByRole('button', { name: 'Destination' }))
  await userEvent.click(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] }))
  if (!supported) {
    await waitFor(() => expect(betaCard()).toHaveTextContent('namespace_transition_unsupported'))
    expect(writes).toEqual([])
    expect(readMeshHandoff(localStorage, 'beta')).toBeNull()
    return
  }
  if (interrupt) {
    await waitFor(() => expect(betaCard()).toHaveTextContent(en['connections.actionRequired']))
    expect(writes).toEqual(['/actions/fleet/control/nodes/detach'])
    first.unmount(); unavailableAfterDetach = false; view()
    await waitFor(() => expect(within(betaCard()).getByRole('button', { name: 'Destination' })).toHaveAttribute('aria-pressed', 'true'))
    await waitFor(() => expect(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] })).toBeEnabled())
    await userEvent.click(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] }))
  }
  await waitFor(() => expect(writes).toEqual(['/actions/fleet/control/nodes/detach', '/actions/fleet/control/nodes/upsert']))
  await waitFor(() => expect(betaCard()).toHaveTextContent(en['connections.confirmed']))
  expect(readMeshHandoff(localStorage, 'beta')).toBeNull()
})
