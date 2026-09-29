import { expect, test, vi } from 'vitest'

import { PairingTransport } from '../auth/transport'
import type { StoredConnection } from '../auth/types'
import {
  BrowserCredentialVault,
  CREDENTIAL_STORAGE_PREFIX,
  STORAGE_KEY,
  type KeyValueStorage,
} from '../auth/vault'
import {
  BrowserConnectionRegistry,
  ConnectionRegistryError,
  REGISTRY_STORAGE_KEY,
} from './registry'

function pairingLink(server: string, name: string, secret: string): string {
  const payload = JSON.stringify({ v: 1, server, name, secret })
  const bytes = new TextEncoder().encode(payload)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  const encoded = btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  return `https://terminal-console.solvenger.app/connect#${encoded}`
}

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

test('migrates the M1 singleton connection into a stable local profile without leaking credential material', () => {
  const storage = new MemoryStorage()
  new BrowserCredentialVault(storage).save(
    connection('https://terminal.example', 'legacy-refresh'),
  )
  const registry = new BrowserConnectionRegistry(storage, () => 2000, ids('instance-a'))

  const [profile] = registry.list()

  expect(profile).toMatchObject({
    instanceId: 'instance-a',
    origin: 'https://terminal.example',
    displayName: 'terminal.example',
    credentialRef: 'profile-instance-a',
    createdAt: 2000,
    updatedAt: 2000,
  })
  expect(storage.getItem(STORAGE_KEY)).toBeNull()
  expect(storage.getItem(REGISTRY_STORAGE_KEY)).not.toContain('legacy-refresh')
  expect(registry.credential('instance-a')?.refreshToken).toBe('legacy-refresh')

  const reloaded = new BrowserConnectionRegistry(storage, () => 9999, ids('unused'))
  expect(reloaded.list()).toEqual([profile])
})

test('stores independent credentials for multiple profiles and restores them after reload', () => {
  const storage = new MemoryStorage()
  const registry = new BrowserConnectionRegistry(
    storage,
    () => 3000,
    ids('alpha', 'beta'),
  )

  const alpha = registry.add(connection('https://alpha.example', 'refresh-a'), 'Alpha')
  const beta = registry.add(connection('https://beta.example', 'refresh-b'), 'Beta')

  expect(registry.list().map((profile) => profile.displayName)).toEqual(['Alpha', 'Beta'])
  expect(registry.credential(alpha.instanceId)?.refreshToken).toBe('refresh-a')
  expect(registry.credential(beta.instanceId)?.refreshToken).toBe('refresh-b')
  expect(storage.getItem(REGISTRY_STORAGE_KEY)).not.toContain('refresh-a')
  expect(storage.getItem(REGISTRY_STORAGE_KEY)).not.toContain('refresh-b')
  expect(
    [...storage.data.keys()].filter((key) => key.startsWith(CREDENTIAL_STORAGE_PREFIX)),
  ).toHaveLength(2)

  const reloaded = new BrowserConnectionRegistry(storage)
  expect(reloaded.list()).toHaveLength(2)
  expect(reloaded.credential(alpha.instanceId)?.origin).toBe('https://alpha.example')
  expect(reloaded.credential(beta.instanceId)?.origin).toBe('https://beta.example')
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

test('pairs from a CLI pairing link, keeps the secret out of storage, and blocks duplicate network exchange', async () => {
  const storage = new MemoryStorage()
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(init?.body)).toContain('one-time-secret')
    return new Response(
      JSON.stringify({
        device_id: 'dev-a',
        client_id: 'client-a',
        access_token: 'short-access',
        token_type: 'Bearer',
        expires_in: 90,
        refresh_token: 'refresh-a',
        scope: 'terminal:read',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  })
  const registry = new BrowserConnectionRegistry(storage, () => 5000, ids('paired-a'))

  const paired = await registry.pairAndAdd(
    pairingLink('https://terminal.example', 'Payload Primary', 'one-time-secret-123456789'),
    'Fleet browser',
    'Primary',
    new PairingTransport(fetcher),
    async () => 'public-key-material-that-is-long-enough',
  )

  expect(paired.profile).toMatchObject({
    instanceId: 'paired-a',
    origin: 'https://terminal.example',
    displayName: 'Primary',
  })
  expect(paired.accessToken).toBe('short-access')
  expect(paired.accessExpiresAt).toBe(95000)
  expect(JSON.stringify([...storage.data.entries()])).not.toContain('one-time-secret')
  expect(JSON.stringify([...storage.data.entries()])).not.toContain('short-access')

  await expect(
    registry.pairAndAdd(
      pairingLink('https://terminal.example', 'Payload Duplicate', 'another-secret-value-123456789'),
      'Fleet browser',
      undefined,
      new PairingTransport(fetcher),
      async () => 'another-public-key-material',
    ),
  ).rejects.toMatchObject({ code: 'duplicate_origin' })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

test('rename persists safe metadata and disconnect removes only the selected profile credentials', () => {
  const storage = new MemoryStorage()
  let now = 1000
  const registry = new BrowserConnectionRegistry(
    storage,
    () => now,
    ids('alpha', 'beta'),
  )
  const alpha = registry.add(connection('https://alpha.example', 'refresh-a'), 'Alpha')
  const beta = registry.add(connection('https://beta.example', 'refresh-b'), 'Beta')

  now = 2000
  expect(registry.rename(alpha.instanceId, '  Renamed Alpha  ')).toMatchObject({
    displayName: 'Renamed Alpha',
    updatedAt: 2000,
  })
  expect(registry.disconnect(alpha.instanceId)).toBe(true)
  expect(registry.get(alpha.instanceId)).toBeNull()
  expect(registry.credential(alpha.instanceId)).toBeNull()
  expect(registry.credential(beta.instanceId)?.refreshToken).toBe('refresh-b')
  expect(registry.disconnect('missing')).toBe(false)
})

test('fails closed on conflicting persisted registry documents', () => {
  const storage = new MemoryStorage()
  storage.setItem(
    REGISTRY_STORAGE_KEY,
    JSON.stringify({
      version: 1,
      profiles: [
        {
          instanceId: 'a',
          origin: 'https://same.example',
          displayName: 'A',
          credentialRef: 'profile-a',
          metadata: {
            deviceId: 'da',
            clientId: 'ca',
            deviceLabel: 'Browser',
            scope: 'terminal:read',
            pairedAt: 1,
          },
          createdAt: 1,
          updatedAt: 1,
        },
        {
          instanceId: 'b',
          origin: 'https://same.example',
          displayName: 'B',
          credentialRef: 'profile-b',
          metadata: {
            deviceId: 'db',
            clientId: 'cb',
            deviceLabel: 'Browser',
            scope: 'terminal:read',
            pairedAt: 2,
          },
          createdAt: 2,
          updatedAt: 2,
        },
      ],
    }),
  )

  expect(() => new BrowserConnectionRegistry(storage).list()).toThrowError(
    new ConnectionRegistryError('conflicting_registry'),
  )
})
