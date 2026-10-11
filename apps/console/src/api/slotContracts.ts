import { ConsoleContractError } from './contracts'
import type {
  AccessMeshDefaultsReadModel, AccessMeshPolicyReadModel, AccessMeshSlotReadModel,
  JsonRecord, ManagedSlotPolicyReadModel, ManagedSlotStatusReadModel,
  ManagedWorkWindowReadModel, PersistentMutationResult,
} from './models'

/** Keep diagnostics to stable public codes; never use server response bodies as messages. */
export function publicErrorCode(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'request_failed'
  const item = value as JsonRecord
  const nested = item.error && typeof item.error === 'object' ? item.error as JsonRecord : {}
  const code = item.code ?? nested.code ?? item.error
  return typeof code === 'string' && /^[a-z][a-z0-9_:-]{0,127}$/.test(code) ? code : 'request_failed'
}

export class ConsoleOperationError extends Error {
  constructor(readonly code: string) {
    super(code)
    this.name = 'ConsoleOperationError'
  }
}

function record(value: unknown, path: string): JsonRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ConsoleContractError(path, 'expected object')
  return value as JsonRecord
}
function text(value: unknown, path: string, nonempty = false): string {
  if (typeof value !== 'string' || (nonempty && !value)) throw new ConsoleContractError(path, 'expected string')
  return value
}
function nullableText(value: unknown, path: string): string | null {
  return value === null ? null : text(value, path)
}
function integer(value: unknown, path: string, min = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < min) throw new ConsoleContractError(path, 'expected safe integer in range')
  return value as number
}
function flag(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') throw new ConsoleContractError(path, 'expected boolean')
  return value
}
function success(value: unknown): JsonRecord {
  const root = record(value, '$')
  if (root.ok === false) throw new ConsoleOperationError(publicErrorCode(root))
  if (root.ok !== true) throw new ConsoleContractError('$.ok', 'expected true')
  return root
}

function managedPolicy(value: unknown, path: string): ManagedSlotPolicyReadModel {
  const item = record(value, path)
  const policy = {
    defaultDurationSeconds: integer(item.default_duration_seconds, path + '.default_duration_seconds', 1),
    warningBeforeExpirySeconds: integer(item.warning_before_expiry_seconds, path + '.warning_before_expiry_seconds'),
    drainingBeforeExpirySeconds: integer(item.draining_before_expiry_seconds, path + '.draining_before_expiry_seconds'),
    rearmAfterSeconds: integer(item.rearm_after_seconds, path + '.rearm_after_seconds'),
    revision: integer(item.revision, path + '.revision', 1),
  }
  if (policy.drainingBeforeExpirySeconds > policy.warningBeforeExpirySeconds) throw new ConsoleContractError(path, 'invalid threshold order')
  return policy
}
function managedWindow(value: unknown, path: string): ManagedWorkWindowReadModel {
  const item = record(value, path)
  return {
    workWindowId: text(item.work_window_id, path + '.work_window_id', true),
    state: text(item.state, path + '.state', true),
    lifecycle: text(item.lifecycle, path + '.lifecycle', true),
    openedAt: text(item.opened_at, path + '.opened_at', true),
    elapsedSeconds: integer(item.elapsed_seconds, path + '.elapsed_seconds'),
    remainingSeconds: integer(item.remaining_seconds, path + '.remaining_seconds'),
    initialDurationSeconds: integer(item.initial_duration_seconds, path + '.initial_duration_seconds', 1),
    effectiveDurationSeconds: integer(item.effective_duration_seconds, path + '.effective_duration_seconds', 1),
    hardExpiresAt: text(item.hard_expires_at, path + '.hard_expires_at', true),
    warningBeforeExpirySeconds: integer(item.warning_before_expiry_seconds, path + '.warning_before_expiry_seconds'),
    drainingBeforeExpirySeconds: integer(item.draining_before_expiry_seconds, path + '.draining_before_expiry_seconds'),
    rearmAfterSeconds: integer(item.rearm_after_seconds, path + '.rearm_after_seconds'),
    rearmAt: nullableText(item.rearm_at, path + '.rearm_at'),
    windowRevision: integer(item.window_revision, path + '.window_revision', 1),
  }
}

export function decodeManagedSlotStatus(value: unknown): ManagedSlotStatusReadModel {
  const root = success(value)
  const rawSession = root.session === null ? null : record(root.session, '$.session')
  return {
    logicalAgentId: text(root.logical_agent_id, '$.logical_agent_id', true),
    publicName: text(root.public_name, '$.public_name', true),
    authorityNodeId: text(root.authority_node_id, '$.authority_node_id', true),
    authorityEpoch: integer(root.authority_epoch, '$.authority_epoch', 1),
    policy: managedPolicy(root.policy, '$.policy'),
    window: root.window === null ? null : managedWindow(root.window, '$.window'),
    session: rawSession === null ? null : {
      workSessionId: text(rawSession.work_session_id, '$.session.work_session_id', true),
      sessionEpoch: integer(rawSession.session_epoch, '$.session.session_epoch', 1),
      role: text(rawSession.role, '$.session.role', true),
      contractVersion: text(rawSession.contract_version, '$.session.contract_version', true),
      state: text(rawSession.state, '$.session.state', true),
      startedAt: text(rawSession.started_at, '$.session.started_at', true),
      endedAt: nullableText(rawSession.ended_at, '$.session.ended_at'),
      endReason: nullableText(rawSession.end_reason, '$.session.end_reason'),
    },
  }
}

function accessPolicy(value: unknown, path: string): AccessMeshPolicyReadModel {
  const item = record(value, path)
  const policy = {
    durationSeconds: integer(item.duration_seconds, path + '.duration_seconds', 1),
    cooldownSeconds: integer(item.cooldown_seconds, path + '.cooldown_seconds'),
    rearmEnabled: flag(item.rearm_enabled, path + '.rearm_enabled'),
    warningSeconds: integer(item.warning_seconds, path + '.warning_seconds'),
    drainingSeconds: integer(item.draining_seconds, path + '.draining_seconds'),
    releaseOnEnd: item.release_on_end === null ? null : flag(item.release_on_end, path + '.release_on_end'),
  }
  if (policy.warningSeconds >= policy.durationSeconds || policy.drainingSeconds >= policy.durationSeconds) throw new ConsoleContractError(path, 'threshold exceeds duration')
  return policy
}

function accessSlot(value: unknown, path: string): AccessMeshSlotReadModel {
  const item = record(value, path)
  if (item.mode !== 'legacy' && item.mode !== 'persistent') throw new ConsoleContractError(path + '.mode', 'unsupported mode')
  const cycle = record(item.session_lifecycle, path + '.session_lifecycle')
  return {
    issuerNodeId: text(item.issuer_node_id, path + '.issuer_node_id', true),
    slotId: text(item.slot_id, path + '.slot_id', true),
    logicalAgentId: text(item.logical_agent_id, path + '.logical_agent_id', true),
    publicName: text(item.public_name, path + '.public_name', true),
    mode: item.mode,
    state: text(item.state, path + '.state', true),
    revision: integer(item.revision, path + '.revision', 1),
    policy: accessPolicy(item.policy, path + '.policy'),
    sessionLifecycle: {
      state: text(cycle.state, path + '.session_lifecycle.state', true),
      startedAt: nullableText(cycle.started_at, path + '.session_lifecycle.started_at'),
      hardExpiresAt: nullableText(cycle.hard_expires_at, path + '.session_lifecycle.hard_expires_at'),
      rearmAt: nullableText(cycle.rearm_at, path + '.session_lifecycle.rearm_at'),
      remainingSeconds: cycle.remaining_seconds === undefined ? undefined : integer(cycle.remaining_seconds, path + '.session_lifecycle.remaining_seconds'),
    },
  }
}

export function decodeAccessMeshSlot(value: unknown): AccessMeshSlotReadModel {
  return accessSlot(success(value).slot, '$.slot')
}
export function decodeAccessMeshSlots(value: unknown): { slots: AccessMeshSlotReadModel[]; nextCursor: string | null } {
  const root = success(value)
  if (!Array.isArray(root.slots)) throw new ConsoleContractError('$.slots', 'expected array')
  return {
    slots: root.slots.map((slot, index) => accessSlot(slot, `$.slots[${index}]`)),
    nextCursor: nullableText(root.next_cursor, '$.next_cursor'),
  }
}
export function decodeAccessMeshDefaults(value: unknown): AccessMeshDefaultsReadModel {
  const root = success(value)
  return {
    revision: integer(root.revision, '$.revision', 1),
    policy: accessPolicy(root.policy, '$.policy'),
    legacyEnabled: flag(root.legacy_enabled, '$.legacy_enabled'),
  }
}

export function decodeSlotMutation(value: unknown): PersistentMutationResult {
  const root = record(value, '$')
  return {
    ok: flag(root.ok, '$.ok'),
    code: root.ok === false ? publicErrorCode(root) : undefined,
    error: root.ok === false ? publicErrorCode(root) : undefined,
    blockers: root.blockers === undefined ? undefined : (
      Array.isArray(root.blockers)
        ? root.blockers.map((item, index) => record(item, `$.blockers[${index}]`))
        : (() => { throw new ConsoleContractError('$.blockers', 'expected array') })()
    ),
    payload: root,
  }
}
