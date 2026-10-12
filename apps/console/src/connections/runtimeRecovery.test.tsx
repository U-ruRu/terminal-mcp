import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { PairingTransport } from '../auth/transport'
import type { KeyValueStorage } from '../auth/vault'
import type { StoredConnection } from '../auth/types'
import { BrowserConnectionRegistry } from './registry'
import { ConnectionRuntimeProvider, useConnectionRuntime } from './runtime'

class MemoryStorage implements KeyValueStorage {
  private readonly data = new Map<string, string>()
  getItem(key: string) { return this.data.get(key) ?? null }
  setItem(key: string, value: string) { this.data.set(key, value) }
  removeItem(key: string) { this.data.delete(key) }
}

function Status() {
  const { profiles, states } = useConnectionRuntime()
  return <output data-testid="connection">{states[profiles[0]?.instanceId]?.status ?? 'missing'}</output>
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('transient HTTP 520 recovers auth without manual restart or re-pairing', async () => {
  const registry = new BrowserConnectionRegistry(new MemoryStorage(), () => 10_000, () => 'fb-id')
  const connection: StoredConnection = {
    origin: 'https://firstbyte.example', deviceId: 'device-fb', clientId: 'client-fb',
    deviceLabel: 'Android console', scope: 'terminal:read',
    refreshToken: 'example', pairedAt: 1_000,
  }
  registry.add(connection, 'FirstByte')
  let attempts = 0
  const fetcher = vi.fn(async () => {
    attempts += 1
    if (attempts === 1) return new Response('{}', { status: 520 })
    return new Response(JSON.stringify({
      access_token: 'example-access', token_type: 'Bearer', expires_in: 120,
      refresh_token: 'example-next', scope: 'terminal:read',
    }), { status: 200 })
  })
  render(
    <ConnectionRuntimeProvider registry={registry} transport={new PairingTransport(fetcher)}>
      <Status />
    </ConnectionRuntimeProvider>,
  )
  await waitFor(() => expect(screen.getByTestId('connection')).toHaveTextContent('connected'), { timeout: 3500 })
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(registry.list()).toHaveLength(1)
})
