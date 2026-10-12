import type { AccessMeshMutationRequest } from '../api/models'

export const SLOT_OPERATION_PREFIX = 'terminal-mcp.console.slot-operation.v1.'
export type PendingSlotOperation = {
  backend: 'access-mesh'
  request: AccessMeshMutationRequest
  startedAt: string
  phase: 'pending' | 'accepted'
} | {
  backend: 'persistent'
  action: 'create' | 'setup' | 'rotate'
  logicalAgentId?: string
  observedGeneration?: number
  startedAt: string
  phase: 'pending' | 'accepted'
}

function key(scope: string): string {
  if (!scope || scope.length > 1000 || /[\r\n\0]/.test(scope)) throw new Error('invalid_operation_scope')
  return SLOT_OPERATION_PREFIX + encodeURIComponent(scope)
}

function plain(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

/** Only request metadata belongs here. Neither responses nor connection codes are journaled. */
function safeRequest(value: unknown): value is AccessMeshMutationRequest {
  if (!plain(value)) return false
  const allowed = new Set(['action', 'idempotency_key', 'slot_id', 'expected_revision', 'mode', 'policy', 'deadline_at', 'legacy_enabled', 'inherit_policy', 'expected_parent_revision'])
  if (Object.keys(value).some((field) => !allowed.has(field))) return false
  if (!['create', 'defaults', 'policy', 'suspend', 'resume', 'delete', 'rotate', 'deadline', 'end'].includes(String(value.action))) return false
  if (typeof value.idempotency_key !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.idempotency_key)) return false
  if (value.slot_id !== undefined && (typeof value.slot_id !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.slot_id))) return false
  if (value.expected_revision !== undefined && (!Number.isSafeInteger(value.expected_revision) || Number(value.expected_revision) < 1)) return false
  if (value.expected_parent_revision !== undefined && (!Number.isSafeInteger(value.expected_parent_revision) || Number(value.expected_parent_revision) < 1 || value.action !== 'policy' || value.inherit_policy !== true)) return false
  if (value.action === 'policy' && value.inherit_policy === true && value.expected_parent_revision === undefined) return false
  if (value.inherit_policy !== undefined && (value.inherit_policy !== true || !['defaults', 'policy'].includes(String(value.action)) || value.policy !== undefined)) return false
  if (value.mode !== undefined && value.mode !== 'persistent' && value.mode !== 'legacy') return false
  if (value.deadline_at !== undefined && (typeof value.deadline_at !== 'string' || value.deadline_at.length > 64 || !Number.isFinite(Date.parse(value.deadline_at)))) return false
  if (value.legacy_enabled !== undefined && typeof value.legacy_enabled !== 'boolean') return false
  if (value.policy !== undefined) {
    if (!plain(value.policy)) return false
    const fields = new Set(['duration_seconds', 'cooldown_seconds', 'rearm_enabled', 'warning_seconds', 'draining_seconds', 'release_on_end'])
    for (const [field, item] of Object.entries(value.policy)) {
      if (!fields.has(field)) return false
      if (field === 'rearm_enabled') { if (typeof item !== 'boolean') return false }
      else if (field === 'release_on_end') { if (item !== null && typeof item !== 'boolean') return false }
      else if (!Number.isSafeInteger(item) || Number(item) < 0) return false
    }
  }
  return true
}

function valid(value: unknown): value is PendingSlotOperation {
  if (!plain(value) || typeof value.startedAt !== 'string' || !Number.isFinite(Date.parse(value.startedAt)) || !['pending', 'accepted'].includes(String(value.phase))) return false
  if (value.backend === 'access-mesh') return Object.keys(value).every((field) => ['backend', 'request', 'startedAt', 'phase'].includes(field)) && safeRequest(value.request)
  return value.backend === 'persistent' && ['create', 'setup', 'rotate'].includes(String(value.action))
    && Object.keys(value).every((field) => ['backend', 'action', 'logicalAgentId', 'observedGeneration', 'startedAt', 'phase'].includes(field))
    && (value.logicalAgentId === undefined || typeof value.logicalAgentId === 'string' && /^[A-Za-z0-9._:-]{1,200}$/.test(value.logicalAgentId))
    && (value.observedGeneration === undefined || Number.isSafeInteger(value.observedGeneration) && Number(value.observedGeneration) >= 0)
}

export function loadSlotOperation(scope: string, storage: Storage = window.localStorage): PendingSlotOperation | null {
  try {
    const raw = storage.getItem(key(scope))
    if (raw === null) return null
    const parsed: unknown = JSON.parse(raw)
    if (!valid(parsed)) throw new Error('invalid')
    return parsed
  } catch { throw new Error('operation_journal_unavailable') }
}

export function beginSlotOperation(scope: string, operation: PendingSlotOperation, storage: Storage = window.localStorage): void {
  if (!valid(operation)) throw new Error('invalid_slot_operation')
  if (loadSlotOperation(scope, storage)) throw new Error('slot_operation_unresolved')
  try { storage.setItem(key(scope), JSON.stringify(operation)) }
  catch { throw new Error('operation_journal_unavailable') }
}

export function acceptSlotOperation(scope: string, storage: Storage = window.localStorage): void {
  const current = loadSlotOperation(scope, storage)
  if (!current) return
  try { storage.setItem(key(scope), JSON.stringify({ ...current, phase: 'accepted' })) }
  catch { throw new Error('operation_journal_unavailable') }
}

export function clearSlotOperation(scope: string, storage: Storage = window.localStorage): void {
  try { storage.removeItem(key(scope)) }
  catch { throw new Error('operation_journal_unavailable') }
}

export function newSlotOperationKey(): string {
  return globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `slot-${Date.now()}-${Math.random().toString(36).slice(2)}`
}
