import { cleanup,render,waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test,vi } from 'vitest'

import { App } from './App'
import { fixtureFleetModel } from './fixtures/fleet'
import { I18nProvider } from './i18n/I18nProvider'
import { fleetInstance,nativeDefaults,nativeSlot,standaloneControl } from './slots/testFixtures'
import { ThemeProvider } from './theme/ThemeProvider'

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear() })

// This exercises the real root routes, rather than mounting ServerSlots directly.
test('server slot route loads the current native slot list', async () => {
  const loadAccessMeshSlots = vi.fn(async () => [nativeSlot()])
  const props = {
    model: fixtureFleetModel, instances: [fleetInstance()],
    loadFleetControl: async () => standaloneControl(),
    loadAccessMeshSlots, loadAccessMeshDefaults: async () => nativeDefaults(),
    mutateAccessMesh: vi.fn(async () => ({ ok: false, payload: {} })),
  }

  render(<I18nProvider><ThemeProvider><MemoryRouter initialEntries={['/servers/alpha/slots']}>
    <App {...props} />
  </MemoryRouter></ThemeProvider></I18nProvider>)

  await waitFor(() => expect(loadAccessMeshSlots).toHaveBeenCalledWith('alpha'))
})
