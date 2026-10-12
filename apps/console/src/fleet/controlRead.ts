import type { ManagedFleetControlReadModel } from '../api/models'

/** Share concurrent read-only requests without treating any old success as a fresh read. */
export function coalesceControlReads(load: (instanceId: string) => Promise<ManagedFleetControlReadModel>) {
  const pending = new Map<string, Promise<ManagedFleetControlReadModel>>()
  return (instanceId: string): Promise<ManagedFleetControlReadModel> => {
    const existing = pending.get(instanceId)
    if (existing) return existing
    const request = Promise.resolve().then(() => load(instanceId)).finally(() => {
      if (pending.get(instanceId) === request) pending.delete(instanceId)
    })
    pending.set(instanceId, request)
    return request
  }
}
