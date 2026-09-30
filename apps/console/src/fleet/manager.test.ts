import { expect, test, vi } from 'vitest'

import type { ActivityFeedReadModel, TaskReadModel } from '../api/models'
import type { ConnectionProfile } from '../connections/types'
import { FleetConnectionManager } from './manager'
import type {
  FleetInstanceActor,
  FleetInstanceRuntimeState,
  FleetRegistry,
} from './types'

function makeProfile(instanceId: string): ConnectionProfile {
  return {
    instanceId,
    origin: 'https://' + instanceId + '.example',
    displayName: instanceId.toUpperCase(),
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

function makeRuntime(
  instanceId: string,
  status: FleetInstanceRuntimeState['status'] = 'offline',
): FleetInstanceRuntimeState {
  return {
    instanceId,
    status,
    authStatus: status === 'live' ? 'connected' : 'unpaired',
    realtime: null,
    reconnectAttempt: 0,
  }
}

class MutableRegistry implements FleetRegistry {
  constructor(public profiles: ConnectionProfile[]) {}

  list(): ConnectionProfile[] {
    return this.profiles.map((item) => ({
      ...item,
      metadata: { ...item.metadata },
    }))
  }
}

class FakeActor implements FleetInstanceActor {
  startCalls = 0
  retryCalls = 0
  stopCalls = 0
  private listeners = new Set<(state: FleetInstanceRuntimeState) => void>()
  private state: FleetInstanceRuntimeState

  constructor(
    readonly instanceId: string,
    private readonly onStart: (actor: FakeActor) => Promise<void> = async () => {},
  ) {
    this.state = makeRuntime(instanceId)
  }

  async start(): Promise<void> {
    this.startCalls += 1
    await this.onStart(this)
  }

  stop(): void {
    this.stopCalls += 1
    this.setState(makeRuntime(this.instanceId))
  }

  async retryNow(): Promise<void> {
    this.retryCalls += 1
  }

  async activity(): Promise<ActivityFeedReadModel> {
    return { events: [], since: 0, nextCursor: 0, highWaterSeq: 0, gap: false }
  }

  async task(namespace: string, taskId: string): Promise<TaskReadModel> {
    return {
      key: namespace + '/' + taskId,
      namespace,
      taskId,
      title: taskId,
      lane: 'implementation',
      priority: 'P2',
      state: 'ready',
      operationalStatus: 'ready',
      active: false,
      archived: false,
      tags: [],
      nextAction: '',
      checkpoint: {},
    }
  }

  async persistentMutation() {
    return { ok: true, payload: { ok: true } }
  }

  getState(): FleetInstanceRuntimeState {
    return this.state
  }

  subscribe(listener: (state: FleetInstanceRuntimeState) => void): () => void {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  setState(state: FleetInstanceRuntimeState): void {
    this.state = state
    for (const listener of this.listeners) listener(state)
  }
}

test('startAll bounds concurrent instance startup without serializing the fleet', async () => {
  const registry = new MutableRegistry(['a', 'b', 'c', 'd'].map(makeProfile))
  let activeStarts = 0
  let peakStarts = 0
  const releases: Array<() => void> = []
  const actors = new Map<string, FakeActor>()

  const manager = new FleetConnectionManager(
    registry,
    (item) => {
      const actor = new FakeActor(
        item.instanceId,
        async () =>
          new Promise<void>((resolve) => {
            activeStarts += 1
            peakStarts = Math.max(peakStarts, activeStarts)
            releases.push(() => {
              activeStarts -= 1
              resolve()
            })
          }),
      )
      actors.set(item.instanceId, actor)
      return actor
    },
    2,
  )

  const starting = manager.startAll()
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  expect(peakStarts).toBe(2)

  releases.splice(0, 2).forEach((release) => release())
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  expect(peakStarts).toBe(2)

  releases.splice(0, 2).forEach((release) => release())
  await starting

  expect(activeStarts).toBe(0)
  expect([...actors.values()].map((actor) => actor.startCalls)).toEqual([1, 1, 1, 1])
})

test('one failed instance does not block healthy peers and runtime state stays isolated', async () => {
  const registry = new MutableRegistry(['bad', 'good'].map(makeProfile))
  const actors = new Map<string, FakeActor>()

  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId, async (self) => {
      if (item.instanceId === 'bad') {
        self.setState({
          ...makeRuntime('bad'),
          authStatus: 'error',
          lastError: 'network_error',
        })
        throw new Error('network_error')
      }
      self.setState(makeRuntime('good', 'live'))
    })
    actors.set(item.instanceId, actor)
    return actor
  })

  await expect(manager.startAll()).resolves.toBeUndefined()

  const views = manager.getInstances()
  expect(views.find((item) => item.profile.instanceId === 'bad')?.runtime).toMatchObject({
    status: 'offline',
    lastError: 'network_error',
  })
  expect(views.find((item) => item.profile.instanceId === 'good')?.runtime.status).toBe('live')
  expect(actors.get('good')?.startCalls).toBe(1)
})

test('foreground recovery retries every paired server without recreating actors', async () => {
  const registry = new MutableRegistry(['a', 'b', 'c'].map(makeProfile))
  const actors = new Map<string, FakeActor>()
  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId)
    actors.set(item.instanceId, actor)
    return actor
  }, 2)

  manager.syncProfiles()
  await manager.recoverAll()
  expect([...actors.values()].map((actor) => actor.retryCalls)).toEqual([1, 1, 1])
  expect([...actors.values()].map((actor) => actor.startCalls)).toEqual([0, 0, 0])
})

test('manual retry targets exactly one server', async () => {
  const registry = new MutableRegistry(['a', 'b'].map(makeProfile))
  const actors = new Map<string, FakeActor>()
  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId)
    actors.set(item.instanceId, actor)
    return actor
  })

  manager.syncProfiles()
  await manager.retry('b')

  expect(actors.get('a')?.retryCalls).toBe(0)
  expect(actors.get('b')?.retryCalls).toBe(1)
  await expect(manager.retry('missing')).rejects.toThrow('profile_not_found:missing')
})

test('profile removal stops only the removed server actor', () => {
  const registry = new MutableRegistry(['a', 'b'].map(makeProfile))
  const actors = new Map<string, FakeActor>()
  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId)
    actors.set(item.instanceId, actor)
    return actor
  })

  manager.syncProfiles()
  registry.profiles = [makeProfile('b')]
  const views = manager.syncProfiles()

  expect(actors.get('a')?.stopCalls).toBe(1)
  expect(actors.get('b')?.stopCalls).toBe(0)
  expect(views.map((item) => item.profile.instanceId)).toEqual(['b'])
})

test('activity delegates to exactly the selected server actor', async () => {
  const registry = new MutableRegistry(['a', 'b'].map(makeProfile))
  const actors = new Map<string, FakeActor>()
  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId)
    actors.set(item.instanceId, actor)
    return actor
  })
  manager.syncProfiles()
  const beta = actors.get('b')!
  const spy = vi.spyOn(beta, 'activity')
  await manager.activity('b', { since: 8, limit: 20 })
  expect(spy).toHaveBeenCalledWith({ since: 8, limit: 20 })
  await expect(manager.activity('missing')).rejects.toThrow('profile_not_found:missing')
})

test('ten-server fixture keeps default startup fan-out at four and isolates failures', async () => {
  const registry = new MutableRegistry(
    Array.from({ length: 10 }, (_, index) => makeProfile('server-' + index)),
  )
  let activeStarts = 0
  let peakStarts = 0
  const releases: Array<() => void> = []
  const actors = new Map<string, FakeActor>()

  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(
      item.instanceId,
      async () =>
        new Promise<void>((resolve, reject) => {
          activeStarts += 1
          peakStarts = Math.max(peakStarts, activeStarts)
          releases.push(() => {
            activeStarts -= 1
            if (item.instanceId === 'server-3') reject(new Error('offline'))
            else resolve()
          })
        }),
    )
    actors.set(item.instanceId, actor)
    return actor
  })

  const starting = manager.startAll()
  await vi.waitFor(() => expect(releases).toHaveLength(4))
  releases.splice(0, 4).forEach((release) => release())
  await vi.waitFor(() => expect(releases).toHaveLength(4))
  releases.splice(0, 4).forEach((release) => release())
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  releases.splice(0, 2).forEach((release) => release())
  await starting

  expect(peakStarts).toBe(4)
  expect(activeStarts).toBe(0)
  expect([...actors.values()].reduce((sum, actor) => sum + actor.startCalls, 0)).toBe(10)
})

test('removed profile is unsubscribed and cannot mutate fleet snapshots afterwards', () => {
  const registry = new MutableRegistry(['a', 'b'].map(makeProfile))
  const actors = new Map<string, FakeActor>()
  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId)
    actors.set(item.instanceId, actor)
    return actor
  })
  const snapshots: string[][] = []

  manager.syncProfiles()
  manager.subscribe((items) => snapshots.push(items.map((item) => item.profile.instanceId)))
  registry.profiles = [makeProfile('b')]
  manager.syncProfiles()
  const afterRemoval = snapshots.length

  actors.get('a')?.setState(makeRuntime('a', 'live'))

  expect(snapshots).toHaveLength(afterRemoval)
  expect(snapshots.at(-1)).toEqual(['b'])
})

test('credential replacement recreates only the affected server actor', () => {
  const initial = makeProfile('a')
  const registry = new MutableRegistry([initial])
  const created: FakeActor[] = []
  const manager = new FleetConnectionManager(registry, (item) => {
    const actor = new FakeActor(item.instanceId)
    created.push(actor)
    return actor
  })

  manager.syncProfiles()
  registry.profiles = [{
    ...initial,
    metadata: { ...initial.metadata, deviceId: 'device-a-repaired', clientId: 'client-a-repaired' },
    updatedAt: 2,
  }]
  manager.syncProfiles()

  expect(created).toHaveLength(2)
  expect(created[0].stopCalls).toBe(1)
  expect(created[1].stopCalls).toBe(0)
  expect(manager.getInstances()[0].profile.metadata.deviceId).toBe('device-a-repaired')
})
