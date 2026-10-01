import type { PersistentMutationResult } from '../api/models'
import type { FleetConfirmedWrite } from './v1Types'

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function integer(value: unknown): number | undefined {
  return Number.isInteger(value) ? Number(value) : undefined
}

export function confirmedWritesFromPersistentMutation(
  sourceNodeId: string,
  path: string,
  body: Record<string, unknown>,
  result: PersistentMutationResult,
  now = Date.now(),
): FleetConfirmedWrite[] {
  if (!result.ok || !sourceNodeId) return []
  const slot = record(result.payload.slot)
  if (!slot) return []
  const entityId = typeof slot.logical_agent_id === 'string'
    ? slot.logical_agent_id
    : typeof body.logical_agent_id === 'string' ? body.logical_agent_id : ''
  const revision = integer(slot.slot_revision)
  if (!entityId || revision === undefined || revision <= 0) return []
  const requestId = typeof body.idempotency_key === 'string'
    ? body.idempotency_key
    : [path, entityId, revision, now].join(':')
  return [{
    requestId,
    sourceNodeId,
    entityType: 'logical_agent',
    entityId,
    authorityEpoch: integer(slot.authority_epoch),
    entityRevision: revision,
    payloadPatch: { ...slot },
    remove: slot.state === 'deleted',
    createdAt: now,
    state: 'confirmed_pending_projection',
  }]
}
