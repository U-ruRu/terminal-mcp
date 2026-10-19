import { cleanup,render,within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test } from 'vitest'
import { fixtureFleetModel } from '../fixtures/fleet'
import type { FleetServerReadModel } from '../fleet/readModel'
import { I18nProvider } from '../i18n/I18nProvider'
import { AppShell } from './AppShell'
afterEach(() => cleanup())
const servers: FleetServerReadModel[] = []
function renderShell(path: string, shellServers: FleetServerReadModel[] = servers) {
  return render(<I18nProvider><MemoryRouter initialEntries={[path]}><AppShell servers={shellServers}><div>Page body</div></AppShell></MemoryRouter></I18nProvider>)
}

test('server navigation binds Tasks and keeps slot selection in the global navigation', () => {
  renderShell('/servers/server-a/tasks', fixtureFleetModel.servers)

  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  const tasks = within(bottom).getByRole('link', { name: /Tasks/ })
  expect(tasks).toHaveAttribute('href', '/servers/server-a/tasks')
  expect(tasks).toHaveAttribute('aria-current', 'page')
  expect(within(bottom).queryByRole('link', { name: /Slots/ })).not.toBeInTheDocument()
  const serverNavigation = document.querySelector('.navigation-context-group') as HTMLElement
  expect(within(serverNavigation).queryByRole('link', { name: /Slots/ })).not.toBeInTheDocument()
})

test.each([
  ['activity', 'Activity'],
  ['slots', 'Slots'],
])('bottom navigation binds the selected Mesh to its %s destination', (section, label) => {
  renderShell('/meshes/mesh-1/' + section, fixtureFleetModel.servers)

  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  const activity = within(bottom).getByRole('link', { name: new RegExp(label) })
  expect(activity).toHaveAttribute('href', '/meshes/mesh-1/' + section)
  expect(activity).toHaveAttribute('aria-current', 'page')
})
