import { ConsoleContractError } from './contracts'
import type {
  AccessMeshDefaultsReadModel, AccessMeshPolicyReadModel, AccessMeshSlotReadModel,
  JsonRecord, ManagedSlotPolicyReadModel, ManagedSlotStatusReadModel,
  ManagedWorkWindowReadModel, PersistentMutationResult, SlotPolicyInheritanceReadModel,
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

function choice<T extends string>(value: unknown, allowed: readonly T[], path: string): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) throw new ConsoleContractError(path, 'unsupported value')
  return value as T
}

export function decodePolicyInheritance(value: unknown, path = '$.inheritance'): SlotPolicyInheritanceReadModel | undefined {
  if (value === undefined) return undefined
  const item = record(value, path)
  const parent = item.parent === null ? null : record(item.parent, path + '.parent')
  return {
    mode: choice(item.mode, ['stored', 'override', 'inherit'] as const, path + '.mode'),
    source: choice(item.source, ['slot', 'mesh', 'standalone'] as const, path + '.source'),
    resetSupported: flag(item.reset_supported, path + '.reset_supported'),
    appliesTo: choice(item.applies_to, ['next_window', 'current_schedule'] as const, path + '.applies_to'),
    pending: flag(item.pending, path + '.pending'),
    parent: parent === null ? null : {
      source: choice(parent.source, ['mesh', 'standalone', 'server'] as const, path + '.parent.source'),
      id: text(parent.id, path + '.parent.id', true),
      revision: parent.revision === null ? null : integer(parent.revision, path + '.parent.revision', 1),
      version: text(parent.version, path + '.parent.version', true),
      freshness: choice(parent.freshness, ['authoritative', 'last_known'] as const, path + '.parent.freshness'),
      convergence: choice(parent.convergence, ['converged', 'pending', 'unknown'] as const, path + '.parent.convergence'),
    },
  }
}

function managedPolicy(value: unknown, path: string): ManagedSlotPolicyReadModel {
  const item = record(value, path)
  const policy = {
    inheritance: decodePolicyInheritance(item.inheritance, path + '.inheritance'),
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
      contractVersion: integer(rawSession.contract_version, '$.session.contract_version', 1),
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

function requireNativeInheritance(item: JsonRecord, path: string, slot: boolean): void {
  if (item.management_contract_version === 2) {
    if (item.inheritance_supported !== true) throw new ConsoleContractError(path + '.inheritance_supported', 'missing v2 capability')
    if (slot) integer(item.code_generation, path + '.code_generation', 1)
  }
  if (item.inheritance_supported !== true) return
  if (!decodePolicyInheritance(item.inheritance, path + '.inheritance')) {
    throw new ConsoleContractError(path + '.inheritance', 'missing supported inheritance metadata')
  }
  accessPolicy(item.inherited_policy, path + '.inherited_policy')
  if (item.policy_override !== null) accessPolicy(item.policy_override, path + '.policy_override')
  choice(item.policy_source, slot ? ['slot', 'mesh', 'server'] : ['mesh', 'server'], path + '.policy_source')
  choice(item.policy_applies_to, ['next_session'], path + '.policy_applies_to')
  if (slot) accessPolicy(item.current_policy, path + '.current_policy')
  else flag(item.mesh_policy_supported, path + '.mesh_policy_supported')
}

function accessSlot(value: unknown, path: string): AccessMeshSlotReadModel {
  const item = record(value, path)
  requireNativeInheritance(item, path, true)
  if (item.mode !== 'legacy' && item.mode !== 'persistent') throw new ConsoleContractError(path + '.mode', 'unsupported mode')
  const cycle = record(item.session_lifecycle, path + '.session_lifecycle')
  return {
    inheritance: decodePolicyInheritance(item.inheritance, path + '.inheritance'),
    managementContractVersion: item.management_contract_version === undefined ? undefined : integer(item.management_contract_version, path + '.management_contract_version', 1),
    inheritanceSupported: item.inheritance_supported === undefined ? undefined : flag(item.inheritance_supported, path + '.inheritance_supported'),
    inheritedPolicy: item.inherited_policy === undefined ? undefined : accessPolicy(item.inherited_policy, path + '.inherited_policy'),
    codeGeneration: item.code_generation === undefined ? undefined : integer(item.code_generation, path + '.code_generation', 1),
    codeExpiresAt: item.code_expires_at === undefined ? undefined : nullableText(item.code_expires_at, path + '.code_expires_at'),
    createdAt: item.created_at === undefined ? undefined : text(item.created_at, path + '.created_at'),
    updatedAt: item.updated_at === undefined ? undefined : text(item.updated_at, path + '.updated_at'),
    windowPolicy: item.current_policy !== undefined ? accessPolicy(item.current_policy, path + '.current_policy') : item.window_policy === undefined ? undefined : accessPolicy(item.window_policy, path + '.window_policy'),
    policyEffectiveAt: item.policy_effective_at === undefined ? undefined : nullableText(item.policy_effective_at, path + '.policy_effective_at'),
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
  requireNativeInheritance(root, '$', false)
  return {
    inheritance: decodePolicyInheritance(root.inheritance),
    managementContractVersion: root.management_contract_version === undefined ? undefined : integer(root.management_contract_version, '$.management_contract_version', 1),
    inheritanceSupported: root.inheritance_supported === undefined ? undefined : flag(root.inheritance_supported, '$.inheritance_supported'),
    meshPolicySupported: root.mesh_policy_supported === undefined ? undefined : flag(root.mesh_policy_supported, '$.mesh_policy_supported'),
    inheritedPolicy: root.inherited_policy === undefined ? undefined : accessPolicy(root.inherited_policy, '$.inherited_policy'),
    writeTarget: root.write_target === undefined ? undefined : choice(root.write_target, ['fleet_control', 'access_defaults'] as const, '$.write_target'),
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
