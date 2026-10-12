import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import type { ManagedFleetControlReadModel } from '../api/models'
import { ConsoleHttpError } from '../api/client'
import { fleetInstance, standaloneControl } from '../slots/testFixtures'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { BrowserDiagnosticJournal } from '../diagnostics/journal'
import { Connections } from './Connections'
import type { useConnectionRuntime } from '../connections/runtime'

const mocked = vi.hoisted(() => ({ value: null as ReturnType<typeof useConnectionRuntime> | null }))
vi.mock('../connections/runtime', () => ({ useConnectionRuntime: () => mocked.value }))
function runtime(load: (id: string) => Promise<ManagedFleetControlReadModel>) {
  mocked.value = { profiles: [fleetInstance().profile, fleetInstance('beta').profile],
    states: Object.fromEntries(['alpha', 'beta'].map((id) => [id, { status: 'connected', accessToken: 'isolated-test-token', accessExpiresAt: 9999999999999 }])),
    error: null, pair: vi.fn(async () => undefined), retry: vi.fn(async () => undefined), disconnect: vi.fn(),
    client: (id) => ({ fleetControl: () => load(id), fleetControlMutation: vi.fn(), fleetEnrollment: vi.fn(), meshVpnStatus: vi.fn(), meshVpnMutation: vi.fn() }),
  }
}
function card(id: string) {
  return [...document.querySelectorAll<HTMLElement>('article.connection-card')].find((item) => item.textContent?.includes(id === 'alpha' ? 'Alpha server' : 'Beta server'))!
}
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })
test('one pending control request does not delay a successful peer or falsely authorize the pending peer', async () => {
  let finish: (value: ManagedFleetControlReadModel) => void = () => undefined
  runtime(async (id) => id === 'alpha' ? standaloneControl() : new Promise((resolve) => { finish = resolve }))
  render(<I18nProvider><MemoryRouter><Connections /></MemoryRouter></I18nProvider>)
  await waitFor(() => expect(within(card('alpha')).getByText(`${en['connections.configurationReady']}: ${en['connections.ready']}`)).toBeInTheDocument())
  expect(within(card('beta')).getByText(`${en['connections.configurationReady']}: ${en['connections.notReady']}`)).toBeInTheDocument()
  await act(async () => { finish(standaloneControl('beta')) })
  expect(within(card('beta')).getByText(`${en['connections.configurationReady']}: ${en['connections.ready']}`)).toBeInTheDocument()
})
test('control timeout is attributed to its exact endpoint and code without copying private error text', async () => {
  runtime(async () => { throw new ConsoleHttpError(0, 'request_timeout', 'private-test-detail', '/actions/fleet/control', 20_000) })
  const diagnostics = new BrowserDiagnosticJournal(localStorage)
  render(<I18nProvider><MemoryRouter><Connections diagnostics={diagnostics} /></MemoryRouter></I18nProvider>)
  await waitFor(() => expect(screen.getAllByText('/actions/fleet/control')).toHaveLength(2))
  expect(card('alpha')).toHaveTextContent('request_timeout')
  expect(card('alpha')).toHaveTextContent('20000 ms')
  expect(document.body).not.toHaveTextContent('private-test-detail')
  expect(localStorage.getItem('terminal-mcp.console.diagnostics.v1')).toContain('elapsed_ms=20000')
  expect(localStorage.getItem('terminal-mcp.console.diagnostics.v1')).not.toContain('private-test-detail')
})
