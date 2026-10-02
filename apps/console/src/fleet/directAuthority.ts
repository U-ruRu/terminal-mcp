import { ConsoleClient, type FetchLike } from '../api/client'
import type { ActivityFeedReadModel, ManagedFleetControlReadModel, ManagedFleetMutationResult, PersistentMutationResult, TaskReadModel } from '../api/models'
import type { ProfileRestoreResult } from '../connections/types'
import { DEFAULT_FLEET_REQUEST_TIMEOUT_MS, withRequestTimeout } from './policy'
import type { FleetActivityOptions } from './types'

export type DirectCredentialSource = { restore(instanceId: string): Promise<ProfileRestoreResult> }

export class BrowserDirectAuthorityClient {
  private readonly fetcher: FetchLike

  constructor(
    private readonly registry: DirectCredentialSource,
    fetcher: FetchLike = fetch,
  ) {
    this.fetcher = withRequestTimeout(fetcher, DEFAULT_FLEET_REQUEST_TIMEOUT_MS)
  }

  fleetControl(instanceId: string): Promise<ManagedFleetControlReadModel> {
    return this.withClient(instanceId, (client) => client.fleetControl())
  }

  fleetControlMutation(
    instanceId: string,
    path: string,
    body: Record<string, unknown>,
  ): Promise<ManagedFleetMutationResult> {
    return this.withClient(instanceId, (client) => client.fleetControlMutation(path, body))
  }

  persistentMutation(
    instanceId: string,
    path: string,
    body: Record<string, unknown>,
  ): Promise<PersistentMutationResult> {
    return this.withClient(instanceId, (client) => client.persistentMutation(path, body))
  }

  task(instanceId: string, namespace: string, taskId: string): Promise<TaskReadModel> {
    return this.withClient(instanceId, (client) => client.task(namespace, taskId))
  }

  activity(instanceId: string, options: FleetActivityOptions = {}): Promise<ActivityFeedReadModel> {
    return this.withClient(instanceId, (client) => client.activity(options))
  }

  private async withClient<T>(
    instanceId: string,
    operation: (client: ConsoleClient) => Promise<T>,
  ): Promise<T> {
    const restored = await this.registry.restore(instanceId)
    if (restored.status !== 'connected') {
      throw new Error('direct_authority_auth_' + restored.status)
    }
    return operation(new ConsoleClient(
      restored.profile.origin,
      () => restored.accessToken,
      this.fetcher,
    ))
  }
}
