import { ConsoleClient, ConsoleHttpError, type FetchLike } from '../api/client'
import type { ActivityFeedReadModel, ManagedFleetControlReadModel, ManagedFleetMutationResult, MeshVpnStatus, MeshVpnResult, TaskReadModel } from '../api/models'
import type { ProfileRestoreResult } from '../connections/types'

type ConnectedRestore = Extract<ProfileRestoreResult, { status: 'connected' }>
const READ_RETRY_DELAYS_MS = [250, 750] as const
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
    return this.withReadRetry(instanceId, (client) => client.fleetControl())
  }

  nodeRuntime(instanceId: string) {
    return this.withReadRetry(instanceId, (client) => client.nodeRuntime())
  }

  nodeRuntimeOperation(instanceId: string, requestId: string) {
    return this.withReadRetry(instanceId, (client) => client.nodeRuntimeOperation(requestId))
  }

  restartNodeRuntime(instanceId: string, target: import('../api/models').NodeRuntimeTarget, requestId: string) {
    return this.withClient(instanceId, (client) => client.restartNodeRuntime(target, requestId))
  }

  meshVpnStatus(instanceId: string): Promise<MeshVpnStatus> {
    return this.withReadRetry(instanceId, (client) => client.meshVpnStatus())
  }

  meshVpnMutation(
    instanceId: string,
    action: 'prepare' | 'enroll' | 'activate' | 'backend' | 'switch' | 'revoke',
    body: Record<string, unknown>,
  ): Promise<MeshVpnResult> {
    return this.withClient(instanceId, (client) => client.meshVpnMutation(action, body))
  }

  fleetEnrollment(instanceId: string) {
    return this.withReadRetry(instanceId, (client) => client.fleetEnrollment())
  }

  fleetControlMutation(
    instanceId: string,
    path: string,
    body: Record<string, unknown>,
  ): Promise<ManagedFleetMutationResult> {
    return this.withClient(instanceId, (client) => client.fleetControlMutation(path, body))
  }

  accessMeshSlots(instanceId: string) {
    return this.withClient(instanceId, (client) => client.accessMeshAllSlots())
  }

  accessMeshDefaults(instanceId: string) {
    return this.withClient(instanceId, (client) => client.accessMeshDefaults())
  }

  accessMeshMutation(instanceId: string, body: import('../api/models').AccessMeshMutationRequest) {
    return this.withClient(instanceId, (client) => client.accessMeshMutation(body))
  }

  task(instanceId: string, namespace: string, taskId: string): Promise<TaskReadModel> {
    return this.withClient(instanceId, (client) => client.task(namespace, taskId))
  }

  activity(instanceId: string, options: FleetActivityOptions = {}): Promise<ActivityFeedReadModel> {
    return this.withClient(instanceId, (client) => client.activity(options))
  }

  // Only read operations may be repeated following a transport failure. A write
  // may have committed before a proxy returned 520, so it must be reconciled.
  private async withReadRetry<T>(
    instanceId: string,
    operation: (client: ConsoleClient) => Promise<T>,
  ): Promise<T> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this.withClient(instanceId, operation)
      } catch (error) {
        if (
          attempt >= READ_RETRY_DELAYS_MS.length
          || !(error instanceof ConsoleHttpError)
          || !error.retryable
        ) throw error
        await new Promise<void>((resolve) => setTimeout(resolve, READ_RETRY_DELAYS_MS[attempt]))
      }
    }
  }

  private async withClient<T>(
    instanceId: string,
    operation: (client: ConsoleClient) => Promise<T>,
  ): Promise<T> {
    const restored = await this.registry.restore(instanceId)
    if (restored.status !== 'connected') {
      if (restored.status === 'error' && restored.retryable) {
        throw new ConsoleHttpError(0, 'direct_authority_auth_transient')
      }
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
