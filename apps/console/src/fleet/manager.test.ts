import { expect,test,vi } from 'vitest'

import type { ActivityFeedReadModel,TaskReadModel } from '../api/models'
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
})

test('credential replacement recreates the actor for that profile', () => {
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
