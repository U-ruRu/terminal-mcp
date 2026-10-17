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

test('bottom navigation binds the selected server to the current Tasks destination', () => {
  renderShell('/servers/server-a/tasks', fixtureFleetModel.servers)

  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  const tasks = within(bottom).getByRole('link', { name: /Tasks/ })
  expect(tasks).toHaveAttribute('href', '/servers/server-a/tasks')
  expect(tasks).toHaveAttribute('aria-current', 'page')
})

test('bottom navigation binds the selected Mesh to the current Activity destination', () => {
  renderShell('/meshes/mesh-1/activity', fixtureFleetModel.servers)

  const bottom = document.querySelector('.mobile-bottom-navigation') as HTMLElement
  const activity = within(bottom).getByRole('link', { name: /Activity/ })
  expect(activity).toHaveAttribute('href', '/meshes/mesh-1/activity')
  expect(activity).toHaveAttribute('aria-current', 'page')
})
