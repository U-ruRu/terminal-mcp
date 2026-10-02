import { ConsoleClient, ConsoleHttpError, type FetchLike } from '../api/client'
import type { ActivityFeedReadModel, ManagedFleetControlReadModel, ManagedFleetMutationResult, PersistentMutationResult, TaskReadModel } from '../api/models'
import type { ProfileRestoreResult } from '../connections/types'

type ConnectedRestore = Extract<ProfileRestoreResult, { status: 'connected' }>
import { DEFAULT_FLEET_REQUEST_TIMEOUT_MS, withRequestTimeout } from './policy'
import type { FleetActivityOptions } from './types'

export type DirectCredentialSource = {
  restore(instanceId: string): Promise<ProfileRestoreResult>
  invalidateAccessSession?(instanceId: string): void
}

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

  fleetEnrollment(instanceId: string) {
    return this.withClient(instanceId, (client) => client.fleetEnrollment())
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
    let session: ConnectedRestore = restored

    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await operation(new ConsoleClient(
          session.profile.origin,
          () => session.accessToken,
          this.fetcher,
        ))
      } catch (error) {
        if (
          attempt > 0
          || !(error instanceof ConsoleHttpError)
          || !error.authenticationRequired
          || !this.registry.invalidateAccessSession
        ) throw error
        this.registry.invalidateAccessSession(instanceId)
        const refreshed = await this.registry.restore(instanceId)
        if (refreshed.status !== 'connected') {
          throw new Error('direct_authority_auth_' + refreshed.status, { cause: error })
        }
        session = refreshed
      }
    }
    throw new Error('direct_authority_auth_retry_exhausted')
  }
}
