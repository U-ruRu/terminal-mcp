import { expect,test,vi } from 'vitest'

import type { KeyValueStorage } from '../auth/vault'
import type { ConnectionProfile } from '../connections/types'
import type { RealtimeScheduler } from '../realtime/engine'
import { BrowserFleetInstanceActor } from './actor'

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

test('retryable authentication failure schedules a bounded jittered retry', async () => {
  const scheduler = new Scheduler()
  const profile = makeProfile('alpha')
  const actor = new BrowserFleetInstanceActor(profile, {
    credentialSource: { restore: vi.fn(async () => ({ status: 'error' as const, profile, retryable: true, message: 'network_error' })) },
    scheduler,
    random: () => 0,
    reconnectBaseMs: 500,
    reconnectMaxMs: 5000,
  })

  await actor.start()

  expect(actor.getState()).toMatchObject({ status: 'reconnecting', authStatus: 'error', reconnectAttempt: 1 })
  expect(scheduler.queue.map((entry) => entry.delayMs)).toEqual([400])
})

test('manual retry cancels pending auth backoff and restores immediately', async () => {
  const scheduler = new Scheduler()
  const profile = makeProfile('alpha')
  const restore = vi.fn(async () => ({ status: 'revoked' as const, profile }))
  const actor = new BrowserFleetInstanceActor(profile, {
    credentialSource: { restore }, scheduler,
  })
  const pending = scheduler.setTimeout(() => {}, 500)
  const state = actor as unknown as { running: boolean; authAttempt: number; authRetryTimer: unknown }
  state.running = true
  state.authAttempt = 3
  state.authRetryTimer = pending

  await actor.retryNow()

  expect(restore.mock.calls).toEqual([['alpha']])
  expect(scheduler.queue).toHaveLength(0)
  expect(actor.getState().reconnectAttempt).toBe(0)
})

test('persistent mutations remain available when realtime reads are stale but auth is connected', async () => {
  const actor = new BrowserFleetInstanceActor(makeProfile('alpha'), { storage: new MemoryStorage() })
  const persistentMutation = vi.fn(async () => ({ ok: false, code: 'revision_conflict' }))
  const applyPersistentMutation = vi.fn(() => undefined)
  const internals = actor as unknown as {
    state: ReturnType<BrowserFleetInstanceActor['getState']>
    client: { persistentMutation: typeof persistentMutation }
    engine: { applyPersistentMutation: typeof applyPersistentMutation }
  }
  internals.state = {
    instanceId: 'alpha',
    status: 'reconnecting',
    authStatus: 'connected',
    realtime: null,
    reconnectAttempt: 1,
  }
  internals.client = { persistentMutation }
  internals.engine = { applyPersistentMutation }

  await expect(
    actor.persistentMutation('/actions/persistent/slots/play', {
      logical_agent_id: 'la_alpha',
      expected_revision: 7,
      idempotency_key: 'actor-stale-write-001',
    }),
  ).resolves.toEqual({ ok: false, code: 'revision_conflict' })
  expect(persistentMutation).toHaveBeenCalledOnce()
  expect(applyPersistentMutation).toHaveBeenCalledWith({ ok: false, code: 'revision_conflict' })
})
