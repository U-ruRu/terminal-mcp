import { cleanup, render, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'
import type { FleetServerReadModel } from '../fleet/readModel'
import { fixtureFleetModel } from '../fixtures/fleet'
import { I18nProvider } from '../i18n/I18nProvider'
import { AppShell } from './AppShell'
afterEach(() => cleanup())
const servers: FleetServerReadModel[] = []
function renderShell(path: string, shellServers: FleetServerReadModel[] = servers) {
  return render(<I18nProvider><MemoryRouter initialEntries={[path]}><AppShell servers={shellServers}><div>Page body</div></AppShell></MemoryRouter></I18nProvider>)
}
test('offers deterministic contextual back navigation for server and task detail routes', () => {
  renderShell('/servers/server-a')
  let appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar).toHaveAttribute('data-variant', 'detail')
  expect(within(appBar).getByRole('link', { name: 'Go back to fleet' })).toHaveAttribute('href', '/')
  cleanup()
  renderShell('/servers/server-a/tasks/core/T-1')
  appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar).toHaveAttribute('data-variant', 'secondary')
  expect(within(appBar).getByRole('link', { name: 'Go back to tasks' })).toHaveAttribute('href', '/servers/server-a/tasks')
})
test('server-scoped activity exposes a direct return path', () => {
  renderShell('/activity?server=server-a')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('link', { name: 'Go back to server' })).toHaveAttribute('href', '/servers/server-a')
})

test('renders both mobile drawer navigation and a separate bottom navigation surface', () => {
  renderShell('/')
  expect(document.querySelector('.menu-toggle')).toBeInTheDocument()
  expect(document.querySelector('.global-navigation')).toBeInTheDocument()
  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  expect(bottom).toBeInTheDocument()
  expect(bottom).not.toBe(document.querySelector('.global-navigation'))
  expect(within(bottom).getAllByRole('link')).toHaveLength(4)
  expect(within(bottom).getByRole('link', { name: /Slots/ })).toHaveAttribute('href', '/slots')
  expect(within(bottom).getByRole('link', { name: /Fleet/ })).toBeInTheDocument()
  expect(within(bottom).getByRole('link', { name: /Settings/ })).toBeInTheDocument()
})

test('root Fleet app bar leaves aggregate status to the Fleet decision surface', () => {
  renderShell('/')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar).toHaveAttribute('data-variant', 'root')
  expect(within(appBar).getByRole('heading', { name: 'Fleet' })).toBeInTheDocument()
  expect(within(appBar).queryByText(/Live 0|Needs attention 0/)).not.toBeInTheDocument()
})


test('global app bars use destination-specific screen identity', () => {
  renderShell('/connections')
  let appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('heading', { name: 'Connections' })).toBeInTheDocument()
  cleanup()
  renderShell('/slots')
  appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('heading', { name: 'Slots' })).toBeInTheDocument()
  cleanup()
  renderShell('/settings')
  appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('heading', { name: 'Settings' })).toBeInTheDocument()
})

test('Mesh entity route has deterministic return to Connections', () => {
  renderShell('/meshes/mesh-1')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('link', { name: 'Go back to connections' })).toHaveAttribute('href', '/connections')
  expect(appBar).toHaveTextContent('Mesh')
})


test('nested Server and Mesh routes resolve through entity hierarchy', () => {
  renderShell('/servers/server-a/agents/agent-1')
  let appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('link', { name: 'Back to agents' })).toHaveAttribute('href', '/servers/server-a/agents')
  cleanup()

  renderShell('/meshes/mesh-1/persistent')
  appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('link', { name: 'Go back to mesh' })).toHaveAttribute('href', '/meshes/mesh-1')
  const contextualNav = document.querySelector('.navigation-context-group') as HTMLElement
  expect(within(contextualNav).getByRole('link', { name: 'Overview' })).toHaveAttribute('href', '/meshes/mesh-1')
  expect(within(contextualNav).getByRole('link', { name: 'Slots' })).toHaveAttribute('href', '/meshes/mesh-1/persistent')
})


test('global status reports the same live population as the Fleet live filter', () => {
  const live = fixtureFleetModel.servers.find((server) => server.instanceId === 'server-c')!
  const loading: FleetServerReadModel = {
    ...live,
    instanceId: 'server-loading',
    displayName: 'Server Loading',
    connectivity: 'connecting',
    connectionState: 'loading',
    freshness: 'loading',
    snapshotAvailable: false,
  }
  renderShell('/connections', [live, loading])
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByText('Live 1')).toBeInTheDocument()
})
