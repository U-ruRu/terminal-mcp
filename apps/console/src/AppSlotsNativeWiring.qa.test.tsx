import { cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import { App } from './App'
import { fixtureFleetModel } from './fixtures/fleet'
import { I18nProvider } from './i18n/I18nProvider'
import { ThemeProvider } from './theme/ThemeProvider'
import { fleetInstance, nativeDefaults, nativeSlot, standaloneControl } from './slots/testFixtures'

afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear() })

// This exercises the real root routes, rather than mounting ServerSlots directly.
test.each(['/slots', '/servers/alpha/slots', '/slots/la_alpha?issuer=alpha&slot=as_alpha', '/servers/alpha/slots/la_alpha?issuer=alpha&slot=as_alpha'])(
  'App forwards native slot readers on the existing route %s', async (path) => {
    const loadAccessMeshSlots = vi.fn(async () => [nativeSlot()])
    const loadAccessMeshDefaults = vi.fn(async () => nativeDefaults())
    const props = {
      model: fixtureFleetModel, instances: [fleetInstance()],
      loadFleetControl: async () => standaloneControl(),
      loadAccessMeshSlots, loadAccessMeshDefaults,
      mutateAccessMesh: vi.fn(async () => ({ ok: false, payload: {} })),
    }
    render(<I18nProvider><ThemeProvider><MemoryRouter initialEntries={[path]}>
      <App {...props} />
    </MemoryRouter></ThemeProvider></I18nProvider>)
    await waitFor(() => expect(loadAccessMeshSlots).toHaveBeenCalledWith('alpha'))
    expect(loadAccessMeshDefaults).toHaveBeenCalledWith('alpha')
  },
)
