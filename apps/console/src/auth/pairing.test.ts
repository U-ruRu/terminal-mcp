import { expect, test, vi } from 'vitest'

import { ConnectionManager } from './pairing'
import { PairingTransport } from './transport'
import { BrowserCredentialVault, STORAGE_KEY, type KeyValueStorage } from './vault'

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

function jsonResponse(body: object, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const exchanged = {
  device_id: 'dev_test',
  client_id: 'device_test',
  access_token: 'short-lived-access',
  token_type: 'Bearer',
  expires_in: 900,
  refresh_token: 'durable-refresh',
  scope: 'terminal:read',
}

test('pairing erases fragment before network and never persists secret or access token', async () => {
  const storage = new MemoryStorage()
  const order: string[] = []
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    order.push('fetch')
    expect(init?.body).toContain('one-time-secret')
    return jsonResponse(exchanged)
  })
  const history = {
    replaceState: vi.fn(() => order.push('erase')),
  }
  const manager = new ConnectionManager(
    new BrowserCredentialVault(storage),
    new PairingTransport(fetcher),
    () => 1000,
    async () => 'public-key-material-that-is-long-enough',
  )

  const state = await manager.pairFromFragment(
    {
      hash: '#one-time-secret',
      origin: 'https://terminal.example',
      pathname: '/connect',
      search: '',
    },
    history,
    'Browser',
  )

  expect(order).toEqual(['erase', 'fetch'])
  expect(history.replaceState).toHaveBeenCalledWith(null, '', '/connect')
  expect(state.status).toBe('connected')
  const persisted = storage.getItem(STORAGE_KEY)!
  expect(persisted).toContain('durable-refresh')
  expect(persisted).not.toContain('one-time-secret')
  expect(persisted).not.toContain('short-lived-access')
})

test('restore rotates refresh material and reconstructs a connected session', async () => {
  const storage = new MemoryStorage()
  const vault = new BrowserCredentialVault(storage)
  vault.save({
    origin: 'https://terminal.example',
    deviceId: 'dev_test',
    clientId: 'device_test',
    deviceLabel: 'Browser',
    scope: 'terminal:read',
    refreshToken: 'old-refresh',
    pairedAt: 1000,
  })
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(init?.body)).toContain('refresh_token=old-refresh')
    return jsonResponse({
      access_token: 'new-access',
      token_type: 'Bearer',
      expires_in: 600,
      refresh_token: 'new-refresh',
      scope: 'terminal:read',
    })
  })
  const manager = new ConnectionManager(vault, new PairingTransport(fetcher), () => 5000)

  const state = await manager.restore()

  expect(state.status).toBe('connected')
  if (state.status !== 'connected') throw new Error('expected connected')
  expect(state.session.accessToken).toBe('new-access')
  expect(state.session.accessExpiresAt).toBe(605000)
  expect(vault.load()?.refreshToken).toBe('new-refresh')
})

test('server-side revoke is detected on restore and clears local credentials', async () => {
  const storage = new MemoryStorage()
  const vault = new BrowserCredentialVault(storage)
  vault.save({
    origin: 'https://terminal.example',
    deviceId: 'dev_test',
    clientId: 'device_test',
    deviceLabel: 'Browser',
    scope: 'terminal:read',
    refreshToken: 'revoked-refresh',
    pairedAt: 1000,
  })
  const manager = new ConnectionManager(
    vault,
    new PairingTransport(async () => jsonResponse({ error: 'invalid_client' }, 401)),
  )

  const state = await manager.restore()

  expect(state.status).toBe('revoked')
  expect(vault.load()).toBeNull()
})

test('expired refresh material is distinguished from server-side device revoke', async () => {
  const storage = new MemoryStorage()
  const vault = new BrowserCredentialVault(storage)
  vault.save({
    origin: 'https://terminal.example',
    deviceId: 'dev_test',
    clientId: 'device_test',
    deviceLabel: 'Browser',
    scope: 'terminal:read',
    refreshToken: 'expired-refresh',
    pairedAt: 1000,
  })
  const manager = new ConnectionManager(
    vault,
    new PairingTransport(async () => jsonResponse({ error: 'invalid_grant' }, 400)),
  )

  const state = await manager.restore()

  expect(state.status).toBe('expired')
  expect(vault.load()).toBeNull()
})

test('transient restore failure preserves refresh material for explicit retry', async () => {
  const storage = new MemoryStorage()
  const vault = new BrowserCredentialVault(storage)
  vault.save({
    origin: 'https://terminal.example',
    deviceId: 'dev_test',
    clientId: 'device_test',
    deviceLabel: 'Browser',
    scope: 'terminal:read',
    refreshToken: 'keep-me',
    pairedAt: 1000,
  })
  const manager = new ConnectionManager(
    vault,
    new PairingTransport(async () => {
      throw new TypeError('offline')
    }),
  )

  const state = await manager.restore()

  expect(state).toMatchObject({
    status: 'error',
    operation: 'restore',
    retryable: true,
    message: 'network_error',
  })
  expect(vault.load()?.refreshToken).toBe('keep-me')
})

test('disconnect forgets local credential material', () => {
  const storage = new MemoryStorage()
  const vault = new BrowserCredentialVault(storage)
  vault.save({
    origin: 'https://terminal.example',
    deviceId: 'dev_test',
    clientId: 'device_test',
    deviceLabel: 'Browser',
    scope: 'terminal:read',
    refreshToken: 'forget-me',
    pairedAt: 1000,
  })
  const manager = new ConnectionManager(vault)

  expect(manager.disconnect()).toEqual({ status: 'disconnected' })
  expect(vault.load()).toBeNull()
})
