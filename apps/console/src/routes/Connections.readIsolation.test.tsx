import { cleanup,render,waitFor,within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'
import type { ManagedFleetControlReadModel } from '../api/models'
import type { useConnectionRuntime } from '../connections/runtime'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { fleetInstance,standaloneControl } from '../slots/testFixtures'
import { Connections } from './Connections'

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
test('a pending peer stays unready while a successful peer becomes configurable', async () => {
  runtime(async (id) => id === 'alpha' ? standaloneControl() : new Promise(() => undefined))

  render(<I18nProvider><MemoryRouter><Connections /></MemoryRouter></I18nProvider>)

  await waitFor(() => expect(within(card('alpha')).getByText(`${en['connections.configurationReady']}: ${en['connections.ready']}`)).toBeInTheDocument())
  expect(within(card('beta')).getByText(`${en['connections.configurationReady']}: ${en['connections.notReady']}`)).toBeInTheDocument()
})
