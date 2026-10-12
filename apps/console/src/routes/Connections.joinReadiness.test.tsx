import { act, cleanup, render, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import { ConsoleHttpError } from '../api/client'
import type { ManagedFleetControlReadModel } from '../api/models'
import type { useConnectionRuntime } from '../connections/runtime'
import { fleetInstance, meshControl, standaloneControl } from '../slots/testFixtures'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { Connections } from './Connections'

const mocked = vi.hoisted(() => ({ value: null as ReturnType<typeof useConnectionRuntime> | null }))
vi.mock('../connections/runtime', () => ({ useConnectionRuntime: () => mocked.value }))
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
function view() { return render(<I18nProvider><MemoryRouter initialEntries={['/meshes/mesh-prod']}><Routes><Route path="/meshes/:meshId" element={<Connections />} /></Routes></MemoryRouter></I18nProvider>) }
function betaCard() { return [...document.querySelectorAll<HTMLElement>('article.connection-card')].find((item) => item.textContent?.includes('Beta server'))! }
function ready(id: string): ManagedFleetControlReadModel {
  return { ...meshControl(id), nativeAccessTrust: { state: 'ready', ready: true, meshId: 'mesh-prod', peers: { [id === 'alpha' ? 'beta' : 'alpha']: 'verified' }, missingTransport: [] } }
}

test.each([[false, false], [true, false], [false, true], [true, true]])('Join waits for own native readiness, uncertain=%s, reopen=%s', async (uncertain, reopen) => {
  let joined = false
  let proofReady = false
  const getControl = async (id: string) => {
    if (id === 'alpha') { const control = ready(id); if (!joined) control.nodes = control.nodes.filter((node) => node.nodeId === 'alpha'); return control }
    if (!joined) return { ...standaloneControl(id), fleetId: 'fleet' }
    const control = ready(id); control.nativeAccessTrust!.ready = proofReady; control.nativeAccessTrust!.state = proofReady ? 'ready' : 'pending'; return control
  }
  const write = vi.fn(async () => { joined = true; if (uncertain) throw new ConsoleHttpError(520, 'proxy_error'); return { ok: true, control: ready('alpha') } })
  mocked.value = { profiles: [fleetInstance().profile, fleetInstance('beta').profile], states: Object.fromEntries(['alpha', 'beta'].map((id) => [id, { status: 'connected', accessToken: 'test-only', accessExpiresAt: 9999999999999 }])), error: null, pair: vi.fn(), retry: vi.fn(), disconnect: vi.fn(), client: (id) => ({ fleetControl: () => getControl(id), fleetControlMutation: write, fleetEnrollment: async () => ({ nodeId: id, origin: `https://${id}.example`, publicKey: 'test-only', authToken: 'test-only' }), meshVpnStatus: vi.fn(), meshVpnMutation: vi.fn() }) }
  const initial = view()
  await waitFor(() => expect(within(betaCard()).getByRole('button', { name: 'Production' })).toBeEnabled())
  await userEvent.click(within(betaCard()).getByRole('button', { name: 'Production' }))
  await userEvent.click(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] }))
  await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(betaCard()).toHaveTextContent(en['connections.applying']))
  expect(betaCard()).not.toHaveTextContent(en['connections.confirmed'])
  await act(async () => { window.dispatchEvent(new Event('online')) })
  expect(write).toHaveBeenCalledTimes(1)
  expect(betaCard()).not.toHaveTextContent(en['connections.confirmed'])
  if (reopen) {
    initial.unmount()
    view()
    await waitFor(() => expect(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] })).toBeEnabled())
    await userEvent.click(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] }))
    await waitFor(() => expect(betaCard()).toHaveTextContent(en['connections.applying']))
    expect(write).toHaveBeenCalledTimes(1)
  }
  proofReady = true
  await act(async () => { window.dispatchEvent(new Event('online')) })
  await waitFor(() => expect(betaCard()).toHaveTextContent(en['connections.confirmed']))
  await userEvent.click(within(betaCard()).getByRole('button', { name: en['connections.applyMembership'] }))
  expect(write).toHaveBeenCalledTimes(1)
})
