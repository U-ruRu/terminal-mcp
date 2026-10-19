import { cleanup,render,screen,waitFor } from '@testing-library/react'
import { MemoryRouter,useLocation } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import { App } from './App'
import { fixtureFleetModel } from './fixtures/fleet'
import { I18nProvider } from './i18n/I18nProvider'
import { fleetInstance,meshControl,nativeDefaults,nativeSlot,standaloneControl } from './slots/testFixtures'
import { ThemeProvider } from './theme/ThemeProvider'

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear() })

// This exercises the real root routes, rather than mounting ServerSlots directly.
function Location() { return <output aria-label="Current route">{useLocation().pathname}</output> }
test.each([
  ['/servers/alpha/slots', false, '/servers/alpha/slots'],
  ['/meshes/mesh-prod/slots', true, '/meshes/mesh-prod/slots'],
  ['/servers/alpha/slots/as_alpha', true, '/meshes/mesh-prod/slots/as_alpha'],
])('slot route %s loads its current registry scope', async (route, shared, canonicalRoute) => {
  const loadAccessMeshSlots = vi.fn(async () => [nativeSlot()])
  const props = {
    model: fixtureFleetModel, instances: [fleetInstance()],
    loadFleetControl: async () => shared ? meshControl() : standaloneControl(),
    loadAccessMeshSlots, loadAccessMeshDefaults: async () => nativeDefaults(),
    mutateAccessMesh: vi.fn(async () => ({ ok: false, payload: {} })),
  }

  render(<I18nProvider><ThemeProvider><MemoryRouter initialEntries={[route]}>
    <App {...props} /><Location />
  </MemoryRouter></ThemeProvider></I18nProvider>)

  await waitFor(() => expect(loadAccessMeshSlots).toHaveBeenCalledWith('alpha'))
  expect(screen.getByLabelText('Current route')).toHaveTextContent(canonicalRoute)
})
