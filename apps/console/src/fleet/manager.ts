import type { ActivityFeedReadModel, PersistentMutationResult, TaskReadModel } from '../api/models'

import type {
  FleetActivityOptions,
  FleetActorFactory,
  FleetInstanceActor,
  FleetInstanceView,
  FleetRegistry,
} from './types'
import { DEFAULT_FLEET_MAX_CONCURRENT_STARTS } from './policy'

type FleetListener = (instances: FleetInstanceView[]) => void

function sameActorCredential(a: FleetInstanceView['profile'], b: FleetInstanceView['profile']): boolean {
  return (
    a.origin === b.origin &&
    a.credentialRef === b.credentialRef &&
    a.metadata.deviceId === b.metadata.deviceId &&
    a.metadata.clientId === b.metadata.clientId
  )
}

export class FleetConnectionManager {
  private readonly actors = new Map<string, FleetInstanceActor>()
  private readonly actorUnsubscribes = new Map<string, () => void>()
  private readonly profiles = new Map<string, FleetInstanceView['profile']>()
  private readonly listeners = new Set<FleetListener>()

  constructor(
    private readonly registry: FleetRegistry,
    private readonly actorFactory: FleetActorFactory,
    private readonly maxConcurrentStarts = DEFAULT_FLEET_MAX_CONCURRENT_STARTS,
  ) {
    if (!Number.isInteger(maxConcurrentStarts) || maxConcurrentStarts < 1) {
      throw new Error('max_concurrent_starts_must_be_positive')
    }
  }

  getInstances(): FleetInstanceView[] {
    return [...this.profiles.values()].map((profile) => {
      const actor = this.actors.get(profile.instanceId)
      if (!actor) throw new Error('missing_actor:' + profile.instanceId)
      return {
        profile: { ...profile, metadata: { ...profile.metadata } },
        runtime: actor.getState(),
      }
    })
  }

  subscribe(listener: FleetListener): () => void {
    this.listeners.add(listener)
    listener(this.getInstances())
    return () => this.listeners.delete(listener)
  }

  syncProfiles(): FleetInstanceView[] {
    const profiles = this.registry.list()
    const wanted = new Set(profiles.map((profile) => profile.instanceId))

    for (const [instanceId, actor] of this.actors) {
      if (wanted.has(instanceId)) continue
      this.actorUnsubscribes.get(instanceId)?.()
      this.actorUnsubscribes.delete(instanceId)
      actor.stop()
      this.actors.delete(instanceId)
      this.profiles.delete(instanceId)
    }

    for (const profile of profiles) {
      const prior = this.profiles.get(profile.instanceId)
      const existingActor = this.actors.get(profile.instanceId)
      if (prior && existingActor && !sameActorCredential(prior, profile)) {
        this.actorUnsubscribes.get(profile.instanceId)?.()
        this.actorUnsubscribes.delete(profile.instanceId)
        existingActor.stop()
        this.actors.delete(profile.instanceId)
      }
      this.profiles.set(profile.instanceId, profile)
      if (this.actors.has(profile.instanceId)) continue
      const actor = this.actorFactory(profile)
      if (actor.instanceId !== profile.instanceId) {
        throw new Error('actor_instance_mismatch:' + profile.instanceId)
      }
      this.actors.set(profile.instanceId, actor)
      this.actorUnsubscribes.set(
        profile.instanceId,
        actor.subscribe(() => this.emit()),
      )
    }

    this.emit()
    return this.getInstances()
  }

  async startAll(): Promise<void> {
    this.syncProfiles()
    const queue = [...this.actors.values()]
    const workers = Array.from(
      { length: Math.min(this.maxConcurrentStarts, queue.length) },
      async () => {
        while (queue.length > 0) {
          const actor = queue.shift()
          if (!actor) return
          try {
            await actor.start()
          } catch {
            // Actor state is authoritative. One failed instance must not stop the fleet.
          }
        }
      },
    )
    await Promise.all(workers)
  }

  async recoverAll(): Promise<void> {
    this.syncProfiles()
    const queue = [...this.actors.values()]
    const workers = Array.from(
      { length: Math.min(this.maxConcurrentStarts, queue.length) },
      async () => {
        while (queue.length > 0) {
          const actor = queue.shift()
          if (!actor) return
          try {
            await actor.retryNow()
          } catch {
            // One failed foreground recovery must not delay healthy peers.
          }
        }
      },
    )
    await Promise.all(workers)
  }

  async activity(
    instanceId: string,
    options: FleetActivityOptions = {},
  ): Promise<ActivityFeedReadModel> {
    const actor = this.actors.get(instanceId)
    if (!actor) throw new Error('profile_not_found:' + instanceId)
    return actor.activity(options)
  }

  async task(instanceId: string, namespace: string, taskId: string): Promise<TaskReadModel> {
    const actor = this.actors.get(instanceId)
    if (!actor) throw new Error('profile_not_found:' + instanceId)
    return actor.task(namespace, taskId)
  }

  async persistentMutation(
    instanceId: string,
    path: string,
    body: Record<string, unknown>,
  ): Promise<PersistentMutationResult> {
    const actor = this.actors.get(instanceId)
    if (!actor) throw new Error('profile_not_found:' + instanceId)
    return actor.persistentMutation(path, body)
  }

  async retry(instanceId: string): Promise<void> {
    const actor = this.actors.get(instanceId)
    if (!actor) throw new Error('profile_not_found:' + instanceId)
    await actor.retryNow()
  }

  stop(instanceId: string): void {
    this.actors.get(instanceId)?.stop()
  }

  stopAll(): void {
    for (const actor of this.actors.values()) actor.stop()
  }

  private emit(): void {
    if (this.listeners.size === 0) return
    const snapshot = this.getInstances()
    for (const listener of this.listeners) listener(snapshot)
  }
}
