import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'
import type { FleetServerReadModel } from '../fleet/readModel'
import { I18nProvider } from '../i18n/I18nProvider'
import { AppShell } from './AppShell'
afterEach(() => cleanup())
const servers: FleetServerReadModel[] = []
function renderShell(path: string) {
  return render(<I18nProvider><MemoryRouter initialEntries={[path]}><AppShell servers={servers}><div>Page body</div></AppShell></MemoryRouter></I18nProvider>)
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
  expect(within(bottom).getAllByRole('link')).toHaveLength(3)
  expect(within(bottom).getByRole('link', { name: /Fleet/ })).toBeInTheDocument()
  expect(within(bottom).getByRole('link', { name: /Settings/ })).toBeInTheDocument()
})

test('root app bar exposes fleet status without duplicating server context', () => {
  renderShell('/')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(appBar).toHaveAttribute('data-variant', 'root')
  expect(within(appBar).getByRole('heading', { name: 'Console' })).toBeInTheDocument()
  expect(screen.getByText('Live 0')).toBeInTheDocument()
})

test('Mesh entity route has deterministic return to Connections', () => {
  renderShell('/meshes/mesh-1')
  const appBar = document.querySelector('.app-bar') as HTMLElement
  expect(within(appBar).getByRole('link', { name: 'Go back to connections' })).toHaveAttribute('href', '/connections')
  expect(appBar).toHaveTextContent('Mesh')
})
