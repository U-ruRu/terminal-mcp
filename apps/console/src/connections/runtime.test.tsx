import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { PairingTransport } from '../auth/transport'
import type { StoredConnection } from '../auth/types'
import type { KeyValueStorage } from '../auth/vault'
import { I18nProvider } from '../i18n/I18nProvider'
import { Connections } from '../routes/Connections'
import { BrowserConnectionRegistry } from './registry'
import { ConnectionRuntimeProvider } from './runtime'

class MemoryStorage implements KeyValueStorage {
  data = new Map<string, string>()

  getItem(key: string) {
    return this.data.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.data.set(key, value)
  }

  removeItem(key: string) {
    this.data.delete(key)
  }
}

function connection(origin: string, suffix: string): StoredConnection {
  return {
    origin,
    deviceId: 'device-' + suffix,
    clientId: 'client-' + suffix,
    deviceLabel: 'Android console',
    scope: 'terminal:read',
    refreshToken: 'refresh-' + suffix,
    pairedAt: 1000,
  }
}

test('restores every stored profile into app runtime and removes only the selected profile', async () => {
  const storage = new MemoryStorage()
  let nextId = 0
  const registry = new BrowserConnectionRegistry(
    storage,
    () => 10_000,
    () => ['alpha', 'beta'][nextId++] ?? 'fallback',
  )
  registry.add(connection('https://alpha.example', 'alpha'), 'Alpha')
  registry.add(connection('https://beta.example', 'beta'), 'Beta')

  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const origin = new URL(String(input)).origin
    const suffix = origin.includes('alpha') ? 'alpha-next' : 'beta-next'
    return new Response(
      JSON.stringify({
        access_token: 'access-' + suffix,
        token_type: 'Bearer',
        expires_in: 120,
        refresh_token: 'refresh-' + suffix,
        scope: 'terminal:read',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  })

  render(
    <I18nProvider>
      <ConnectionRuntimeProvider registry={registry} transport={new PairingTransport(fetcher)}>
        <Connections />
      </ConnectionRuntimeProvider>
    </I18nProvider>,
  )

  expect(screen.getByRole('heading', { name: 'Connections' })).toBeInTheDocument()
  expect(screen.getByText('Alpha')).toBeInTheDocument()
  expect(screen.getByText('Beta')).toBeInTheDocument()
  await waitFor(() => expect(screen.getAllByText('Connected')).toHaveLength(2))
  expect(fetcher).toHaveBeenCalledTimes(2)

  const removeButtons = screen.getAllByRole('button', { name: 'Remove' })
  await userEvent.click(removeButtons[0])

  await waitFor(() => expect(registry.list().map((profile) => profile.displayName)).toEqual(['Beta']))
  expect(screen.getByText('1 saved')).toBeInTheDocument()
  expect(screen.getAllByText('Connected')).toHaveLength(1)
  expect(registry.credential('beta')?.refreshToken).toBe('refresh-beta-next')
})

afterEach(() => cleanup())
