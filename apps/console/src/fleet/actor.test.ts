import { expect, test, vi } from 'vitest'

import { BrowserCredentialVault, type KeyValueStorage } from '../auth/vault'
import type { ConnectionProfile } from '../connections/types'
import type { RealtimeScheduler } from '../realtime/engine'
import { BrowserFleetInstanceActor, jitterDelay } from './actor'

class MemoryStorage implements KeyValueStorage {
  data = new Map<string, string>()

  getItem(key: string): string | null {
    return this.data.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.data.set(key, value)
  }

  removeItem(key: string): void {
    this.data.delete(key)
  }
}

class Scheduler implements RealtimeScheduler {
  queue: Array<{ callback: () => void; delayMs: number; handle: object }> = []

  setTimeout(callback: () => void, delayMs: number): unknown {
    const handle = {}
    this.queue.push({ callback, delayMs, handle })
    return handle
  }

  clearTimeout(handle: unknown): void {
    this.queue = this.queue.filter((item) => item.handle !== handle)
  }
}

function makeProfile(instanceId: string): ConnectionProfile {
  return {
    instanceId,
    origin: 'https://' + instanceId + '.example',
    displayName: instanceId,
    credentialRef: 'credential-' + instanceId,
    metadata: {
      deviceId: 'device-' + instanceId,
      clientId: 'client-' + instanceId,
      deviceLabel: 'Console ' + instanceId,
      scope: 'terminal:read',
      pairedAt: 1,
    },
    createdAt: 1,
    updatedAt: 1,
  }
}

function seedCredential(
  storage: KeyValueStorage,
  item: ConnectionProfile,
  refreshToken: string,
): void {
  BrowserCredentialVault.forReference(item.credentialRef, storage).save({
    origin: item.origin,
    deviceId: item.metadata.deviceId,
    clientId: item.metadata.clientId,
    deviceLabel: item.metadata.deviceLabel,
    scope: item.metadata.scope,
    refreshToken,
    pairedAt: item.metadata.pairedAt,
  })
}

function jsonResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

test('jitterDelay applies a bounded symmetric jitter window', () => {
  expect(jitterDelay(1000, () => -1)).toBe(800)
  expect(jitterDelay(1000, () => 0.5)).toBe(1000)
  expect(jitterDelay(1000, () => 2)).toBe(1200)
})

test('transient auth failure backs off with jitter and manual retry is immediate', async () => {
  const storage = new MemoryStorage()
  const item = makeProfile('alpha')
  seedCredential(storage, item, 'refresh-alpha')
  const scheduler = new Scheduler()
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new TypeError('offline'))
    .mockResolvedValueOnce(jsonResponse({ error: 'invalid_client' }, 401))

  const actor = new BrowserFleetInstanceActor(item, {
    storage,
    fetcher,
    scheduler,
    random: () => 0,
    reconnectBaseMs: 500,
    reconnectMaxMs: 5000,
  })

  await actor.start()

  expect(actor.getState()).toMatchObject({
    status: 'reconnecting',
    authStatus: 'error',
    reconnectAttempt: 1,
    lastError: 'Cannot reach the server. Check the server address and network connection. Diagnostic: network_error.',
  })
  expect(scheduler.queue.map((entry) => entry.delayMs)).toEqual([400])

  await actor.retryNow()

  expect(scheduler.queue).toHaveLength(0)
  expect(actor.getState()).toMatchObject({
    status: 'offline',
    authStatus: 'revoked',
    reconnectAttempt: 0,
    lastError: 'auth_revoked',
  })
  expect(fetcher).toHaveBeenCalledTimes(2)
})

test('actors use independent credential namespaces with shared storage', async () => {
  const storage = new MemoryStorage()
  const alpha = makeProfile('alpha')
  const beta = makeProfile('beta')
  seedCredential(storage, alpha, 'refresh-alpha')
  seedCredential(storage, beta, 'refresh-beta')

  const bodies: string[] = []
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(String(init?.body))
    return jsonResponse({ error: 'invalid_client' }, 401)
  })
  const actorAlpha = new BrowserFleetInstanceActor(alpha, { storage, fetcher })
  const actorBeta = new BrowserFleetInstanceActor(beta, { storage, fetcher })

  await actorAlpha.start()
  expect(
    BrowserCredentialVault.forReference(beta.credentialRef, storage).load()?.refreshToken,
  ).toBe('refresh-beta')
  await actorBeta.start()

  expect(bodies).toHaveLength(2)
  expect(bodies[0]).toContain('refresh-alpha')
  expect(bodies[0]).not.toContain('refresh-beta')
  expect(bodies[1]).toContain('refresh-beta')
  expect(bodies[1]).not.toContain('refresh-alpha')
})

test('persistent mutations fail closed without an authenticated write client', async () => {
  const actor = new BrowserFleetInstanceActor(makeProfile('alpha'), { storage: new MemoryStorage() })
  await expect(actor.persistentMutation('/actions/persistent/slots/play', {})).rejects.toThrow(
    'instance_not_connected:alpha',
  )
})

test('persistent mutations remain available when realtime reads are stale but auth is connected', async () => {
  const actor = new BrowserFleetInstanceActor(makeProfile('alpha'), { storage: new MemoryStorage() })
  const persistentMutation = vi.fn(async () => ({ ok: false, code: 'revision_conflict' }))
  const refreshNow = vi.fn(async () => undefined)
  const internals = actor as unknown as {
    state: ReturnType<BrowserFleetInstanceActor['getState']>
    client: { persistentMutation: typeof persistentMutation }
    engine: { refreshNow: typeof refreshNow }
  }
  internals.state = {
    instanceId: 'alpha',
    status: 'reconnecting',
    authStatus: 'connected',
    realtime: null,
    reconnectAttempt: 1,
  }
  internals.client = { persistentMutation }
  internals.engine = { refreshNow }

  await expect(
    actor.persistentMutation('/actions/persistent/slots/play', {
      logical_agent_id: 'la_alpha',
      expected_revision: 7,
      idempotency_key: 'actor-stale-write-001',
    }),
  ).resolves.toEqual({ ok: false, code: 'revision_conflict' })
  expect(persistentMutation).toHaveBeenCalledOnce()
  expect(refreshNow).toHaveBeenCalledOnce()
})
