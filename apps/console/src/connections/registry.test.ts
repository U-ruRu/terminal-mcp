import { expect,test,vi } from 'vitest'

import { ConnectionManager } from '../auth/pairing'
import { PairingTransport } from '../auth/transport'
import type { StoredConnection } from '../auth/types'
import {
BrowserCredentialVault,
CREDENTIAL_STORAGE_PREFIX,
type KeyValueStorage
} from '../auth/vault'
import {
BrowserConnectionRegistry,
ConnectionRegistryError
} from './registry'

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

function connection(origin: string, refreshToken: string): StoredConnection {
  const host = new URL(origin).hostname.replaceAll('.', '-')
  return {
    origin,
    deviceId: 'device-' + host,
    clientId: 'client-' + host,
    deviceLabel: 'Browser',
    scope: 'terminal:read',
    refreshToken,
    pairedAt: 1000,
  }
}

function ids(...values: string[]) {
  let index = 0
  return () => values[index++] ?? 'fallback-' + index
}

function jsonResponse(body: object, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('adding a profile stores its credentials in an independent namespace', () => {
  const storage = new MemoryStorage()
  const registry = new BrowserConnectionRegistry(storage, () => 3000, ids('alpha', 'beta'))
  const alpha = registry.add(connection('https://alpha.example', 'refresh-a'), 'Alpha')

  const beta = registry.add(connection('https://beta.example', 'refresh-b'), 'Beta')

  expect(registry.credential(alpha.instanceId)?.refreshToken).toBe('refresh-a')
  expect(registry.credential(beta.instanceId)?.refreshToken).toBe('refresh-b')
  expect([...storage.data.keys()].filter((key) => key.startsWith(CREDENTIAL_STORAGE_PREFIX))).toHaveLength(2)
})
test('coalesces concurrent refresh across registry and realtime auth consumers', async () => {
  const storage = new MemoryStorage()
  const registry = new BrowserConnectionRegistry(storage, () => 5000, ids('alpha'))
  const profile = registry.add(connection('https://alpha.example', 'refresh-a'), 'Alpha')
  const vault = BrowserCredentialVault.forReference(profile.credentialRef, storage)

  let releaseRefresh!: () => void
  let markStarted!: () => void
  const refreshStarted = new Promise<void>((resolve) => { markStarted = resolve })
  const refreshGate = new Promise<void>((resolve) => { releaseRefresh = resolve })
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(init?.body)).toContain('refresh_token=refresh-a')
    markStarted()
    await refreshGate
    return jsonResponse({
      access_token: 'access-b',
      token_type: 'Bearer',
      expires_in: 120,
      refresh_token: 'refresh-b',
      scope: 'terminal:read',
    })
  })
  const transport = new PairingTransport(fetcher)
  const manager = new ConnectionManager(vault, transport, () => 5000)

  const registryRestore = registry.restore(profile.instanceId, transport)
  await refreshStarted
  const actorRestore = manager.restore()
  expect(fetcher).toHaveBeenCalledTimes(1)

  releaseRefresh()
  const [registryState, actorState] = await Promise.all([registryRestore, actorRestore])

  expect(registryState.status).toBe('connected')
  expect(actorState.status).toBe('connected')
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(vault.load()?.refreshToken).toBe('refresh-b')
})

test('rejects duplicate canonical origins before overwriting existing profile credentials', () => {
  const storage = new MemoryStorage()
  const registry = new BrowserConnectionRegistry(storage, () => 3000, ids('alpha', 'beta'))
  const first = registry.add(connection('https://EXAMPLE.com', 'keep-me'))

  expect(() =>
    registry.add(connection('https://example.com/', 'replace-me')),
  ).toThrowError(new ConnectionRegistryError('duplicate_origin'))
  expect(registry.list()).toHaveLength(1)
  expect(registry.credential(first.instanceId)?.refreshToken).toBe('keep-me')
})

test.each(['terminal:read terminal:execute', 'terminal:read'])(
  'refresh replaces saved profile authorization scope only with the granted value: %s', async (granted) => {
    const storage = new MemoryStorage()
    const registry = new BrowserConnectionRegistry(storage, () => 2000, ids('scoped'))
    const original = { ...connection('https://scoped.example', 'isolated-refresh'),
      scope: granted === 'terminal:read' ? 'terminal:read terminal:execute' : 'terminal:read' }
    const profile = registry.add(original, 'Scoped server')
    const fetcher = vi.fn(async () => jsonResponse({ access_token: 'isolated-access', refresh_token: 'isolated-rotated', expires_in: 3600, token_type: 'Bearer', scope: granted }))
    const transport = new PairingTransport(fetcher)
    const restored = await registry.restore(profile.instanceId, transport)
    expect(restored.status).toBe('connected')
    if (restored.status !== 'connected') throw new Error('expected connected test session')
    expect(restored.profile.metadata.scope).toBe(granted)
    expect(registry.get(profile.instanceId)?.metadata.scope).toBe(granted)
    expect(restored.profile.metadata.pairedAt).toBe(original.pairedAt)
    expect(registry.get(profile.instanceId)?.credentialRef).toBe(profile.credentialRef)
    expect(fetcher).toHaveBeenCalledTimes(1)
  },
)
