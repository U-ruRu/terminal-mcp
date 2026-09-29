import type { ActivityFeedReadModel, TaskReadModel } from '../api/models'
import type { ConnectionState } from '../auth/types'
import type { ConnectionProfile } from '../connections/types'
import type { RealtimeState, RealtimeStatus } from '../realtime/state'

export type FleetInstanceRuntimeState = {
  instanceId: string
  status: RealtimeStatus
  authStatus: ConnectionState['status']
  realtime: RealtimeState | null
  reconnectAttempt: number
  lastError?: string
}

export type FleetInstanceView = {
  profile: ConnectionProfile
  runtime: FleetInstanceRuntimeState
}

export type FleetActivityOptions = { since?: number; before?: number; limit?: number; eventTypes?: string[]; entityTypes?: string[] }

export type FleetInstanceActor = {
  readonly instanceId: string
  start(): Promise<void>
  stop(): void
  retryNow(): Promise<void>
  activity(options?: FleetActivityOptions): Promise<ActivityFeedReadModel>
  task(namespace: string, taskId: string): Promise<TaskReadModel>
  getState(): FleetInstanceRuntimeState
  subscribe(listener: (state: FleetInstanceRuntimeState) => void): () => void
}

export type FleetActorFactory = (profile: ConnectionProfile) => FleetInstanceActor
export type FleetRegistry = { list(): ConnectionProfile[] }
