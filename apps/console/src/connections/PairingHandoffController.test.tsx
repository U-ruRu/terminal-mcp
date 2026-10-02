import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'

import { I18nProvider } from '../i18n/I18nProvider'
import { PRODUCTION_CONSOLE_ORIGIN } from './pairingLink'
import { PairingHandoffController } from './PairingHandoffController'

const native = vi.hoisted(() => ({
  listener: undefined as ((event: { url: string }) => void) | undefined,
  pair: vi.fn(async () => undefined),
}))

vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: () => true },
}))

vi.mock('@capacitor/app', () => ({
  App: {
    addListener: vi.fn(async (_event: string, listener: (event: { url: string }) => void) => {
      native.listener = listener
      return { remove: vi.fn(async () => undefined) }
    }),
    getLaunchUrl: vi.fn(async () => undefined),
  },
}))

vi.mock('./runtime', () => ({
  useConnectionRuntime: () => ({ pair: native.pair }),
}))

afterEach(() => {
  cleanup()
  native.listener = undefined
  native.pair.mockClear()
})

function pairingLink(): string {
  const payload = JSON.stringify({
    v: 1,
    server: 'https://secondary.example',
    name: 'Secondary',
    secret: 'one-time-secret-123456789',
  })
  const bytes = new TextEncoder().encode(payload)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  const encoded = btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  return PRODUCTION_CONSOLE_ORIGIN + '/connect#' + encoded
}

function LocationProbe() {
  const location = useLocation()
  return <output aria-label="location">{location.pathname + location.search}</output>
}

test('native pairing handoff overlays the current route and cancel restores focus', async () => {
  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/activity?server=secondary']}>
        <button type="button">Reading anchor</button>
        <LocationProbe />
        <PairingHandoffController />
      </MemoryRouter>
    </I18nProvider>,
  )

  const anchor = screen.getByRole('button', { name: 'Reading anchor' })
  anchor.focus()
  await waitFor(() => expect(native.listener).toBeTypeOf('function'))

  await act(async () => native.listener?.({ url: pairingLink() }))

  expect(screen.getByRole('dialog', { name: 'Pair Terminal MCP' })).toHaveAttribute('aria-modal', 'true')
  expect(screen.getByLabelText('location')).toHaveTextContent('/activity?server=secondary')
  expect(anchor).toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByLabelText('location')).toHaveTextContent('/activity?server=secondary')
  await waitFor(() => expect(anchor).toHaveFocus())
})
