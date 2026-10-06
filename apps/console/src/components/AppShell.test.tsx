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
test('keeps the navigation menu on the left and omits an in-app Back control on contextual routes', () => {
  renderShell('/servers/server-a')
  let appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar).toHaveAttribute('data-variant', 'detail')
  expect(within(appBar).getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Go back/ })).not.toBeInTheDocument()
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(appBar.querySelector('.app-bar-trailing .menu-toggle')).not.toBeInTheDocument()
  cleanup()
  renderShell('/servers/server-a/tasks/core/T-1')
  appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar).toHaveAttribute('data-variant', 'secondary')
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Go back/ })).not.toBeInTheDocument()
})
test('server-scoped activity keeps the menu leading instead of rendering a Back control', () => {
  renderShell('/activity?server=server-a')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Go back/ })).not.toBeInTheDocument()
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
  expect(document.querySelector('.navigation-version')).toHaveTextContent('APK 0.2.30 · code 32')
})


test('bottom navigation follows the selected server context', () => {
  renderShell('/servers/server-a/tasks', fixtureFleetModel.servers)
  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  const links = within(bottom).getAllByRole('link')
  expect(links).toHaveLength(7)
  expect(within(bottom).getByRole('link', { name: /Overview/ })).toHaveAttribute('href', '/servers/server-a')
  expect(within(bottom).getByRole('link', { name: /Agents/ })).toHaveAttribute('href', '/servers/server-a/agents')
  expect(within(bottom).getByRole('link', { name: /Slots/ })).toHaveAttribute('href', '/servers/server-a/slots')
  expect(within(bottom).getByRole('link', { name: /Tasks/ })).toHaveAttribute('href', '/servers/server-a/tasks')
  expect(within(bottom).getByRole('link', { name: /Activity/ })).toHaveAttribute('href', '/activity?server=server-a')
  expect(within(bottom).getByRole('link', { name: /Context/ })).toHaveAttribute('href', '/servers/server-a/context')
  expect(within(bottom).getByRole('link', { name: /Diagnostics/ })).toHaveAttribute('href', '/servers/server-a/health')
  expect(within(bottom).getByRole('link', { name: /Tasks/ })).toHaveAttribute('aria-current', 'page')
})

test('bottom navigation follows Mesh context and omits server-only destinations', () => {
  renderShell('/meshes/mesh-1/activity', fixtureFleetModel.servers)
  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  const links = within(bottom).getAllByRole('link')
  expect(links).toHaveLength(5)
  expect(within(bottom).getByRole('link', { name: /Overview/ })).toHaveAttribute('href', '/meshes/mesh-1')
  expect(within(bottom).getByRole('link', { name: /Agents/ })).toHaveAttribute('href', '/meshes/mesh-1/agents')
  expect(within(bottom).getByRole('link', { name: /Slots/ })).toHaveAttribute('href', '/meshes/mesh-1/persistent')
  expect(within(bottom).getByRole('link', { name: /Tasks/ })).toHaveAttribute('href', '/meshes/mesh-1/tasks')
  expect(within(bottom).getByRole('link', { name: /Activity/ })).toHaveAttribute('href', '/meshes/mesh-1/activity')
  expect(within(bottom).queryByRole('link', { name: /Context/ })).not.toBeInTheDocument()
  expect(within(bottom).queryByRole('link', { name: /Diagnostics/ })).not.toBeInTheDocument()
  expect(within(bottom).getByRole('link', { name: /Activity/ })).toHaveAttribute('aria-current', 'page')
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

test('Mesh entity route keeps the leading menu and contextual title without an in-app Back control', () => {
  renderShell('/meshes/mesh-1')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Go back/ })).not.toBeInTheDocument()
  expect(appBar).toHaveTextContent('Mesh')
})


test('nested Server and Mesh routes keep entity hierarchy in the drawer while the app bar stays menu-first', () => {
  renderShell('/servers/server-a/agents/agent-1')
  let appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Back to|Go back/ })).not.toBeInTheDocument()
  cleanup()

  renderShell('/meshes/mesh-1/persistent')
  appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Back to|Go back/ })).not.toBeInTheDocument()
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


test('entity returnTo state never reintroduces a top-bar Back control', () => {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={[{ pathname: '/servers/server-a/agents/agent-1', state: { returnTo: '/servers/server-a/tasks/core/T-1' } }]}>
        <AppShell servers={[]}><div>Agent</div></AppShell>
      </MemoryRouter>
    </I18nProvider>,
  )
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar.querySelector('.app-bar-leading .menu-toggle')).toBeInTheDocument()
  expect(within(appBar).queryByRole('link', { name: /Back to|Go back/ })).not.toBeInTheDocument()
})
