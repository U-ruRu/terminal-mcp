import { useEffect, useState } from 'react'
import { Link, Navigate, useLocation, useParams } from 'react-router-dom'

import type { ManagedFleetControlReadModel, ManagedFleetMutationResult, PersistentAuditReadModel, PersistentConsoleReadModel, PersistentMutationResult, PersistentPolicyReadModel, PersistentSlotReadModel } from '../api/models'
import { clearAccessCode, loadAccessCode, saveAccessCode } from '../access/codeVault'
import { loadCachedFleetControlForProfile, propagateCachedFleetControl, saveCachedFleetControl, type FleetControlFreshness } from '../connections/controlState'
import type { FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { meshPersistentSlotRoute, slotRoute, taskRoute } from '../navigation/routes'
import { FeedbackState } from '../components/UiPrimitives'

export type PersistentMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>
export type FleetControlLoader = (instanceId: string) => Promise<ManagedFleetControlReadModel>
export type FleetControlMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<ManagedFleetMutationResult>
function idempotencyKey(): string { return globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `console-${Date.now()}-${Math.random().toString(36).slice(2)}` }
const slotStateKeys: Record<string, MessageKey> = {
  suspended: 'slots.state.suspended', armed: 'slots.state.armed', active: 'slots.state.active',
  stopping: 'slots.state.stopping', ended: 'slots.state.ended', expired: 'slots.state.expired',
  deleting: 'slots.state.deleting', deleted: 'slots.state.deleted',
}
function localizedSlotState(t: (key: MessageKey) => string, state: string): string {
  return t(slotStateKeys[state] ?? 'slots.state.unknown')
}
const claimStateKeys: Record<string, MessageKey> = {
  ready: 'slots.claimState.ready', blocked: 'slots.claimState.blocked', deferred: 'slots.claimState.deferred',
  done: 'slots.claimState.done', active: 'slots.claimState.inProgress', in_progress: 'slots.claimState.inProgress',
}
function localizedClaimState(t: (key: MessageKey) => string, state: string): string {
  return t(claimStateKeys[state] ?? 'slots.claimState.unknown')
}
const runtimeStateKeys: Record<string, MessageKey> = {
  fresh: 'status.fresh', catching_up: 'status.catchingUp', live: 'status.live', stale: 'status.stale',
  offline: 'status.offline', reconnecting: 'status.reconnecting', connecting: 'status.connecting',
  loading: 'status.loading', attention: 'status.attention',
}
function localizedRuntimeState(t: (key: MessageKey) => string, state: string): string {
  return t(runtimeStateKeys[state] ?? 'slots.state.unknown')
}
function localizedAuditEvent(t: (key: MessageKey) => string, eventType: string): string {
  const key: MessageKey = eventType === 'session_start' || eventType === 'work_session.created'
    ? 'slots.auditEvent.sessionStarted'
    : eventType === 'slot.played'
      ? 'slots.auditEvent.madeAvailable'
      : eventType === 'claim_released'
        ? 'slots.auditEvent.claimReleased'
        : 'slots.auditEvent.activity'
  return t(key)
}
function normalizedOrigin(value: string | undefined): string { return (value ?? '').replace(/\/+$/, '').toLowerCase() }

type SlotScopeContext = {
  key: string
  kind: 'mesh' | 'server'
  label: string
  meshId?: string
  instanceId: string
  memberInstanceIds: string[]
  available: boolean
  error?: string
}

type SlotEntry = {
  slot: PersistentSlotReadModel
  instanceId: string
  ownerKey: string
  authorityMatch: boolean
  policy: PersistentPolicyReadModel
  persistent: PersistentConsoleReadModel
}

type DurationUnit = 'seconds' | 'minutes' | 'hours'
type DurationValue = { amount: string; unit: DurationUnit }
type TimingDraft = { key: string; duration: DurationValue; warning: DurationValue; alert: DurationValue; rearm: DurationValue; legacyAdmissionEnabled: boolean }

function durationValue(seconds: number): DurationValue {
  if (seconds > 0 && seconds % 3600 === 0) return { amount: String(seconds / 3600), unit: 'hours' }
  if (seconds > 0 && seconds % 60 === 0) return { amount: String(seconds / 60), unit: 'minutes' }
  return { amount: String(seconds), unit: 'seconds' }
}

function durationSecondsValue(value: DurationValue): number | null {
  if (!/^\d+$/.test(value.amount.trim())) return null
  const amount = Number(value.amount)
  if (!Number.isSafeInteger(amount)) return null
  const multiplier = value.unit === 'hours' ? 3600 : value.unit === 'minutes' ? 60 : 1
  const seconds = amount * multiplier
  return Number.isSafeInteger(seconds) ? seconds : null
}

function formatSessionTimer(seconds: number, locale: ReturnType<typeof useI18n>['locale']): string {
  const safe = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const secs = safe % 60
  const suffix = locale === 'ru'
    ? { h: 'ч', m: 'м', s: 'с' }
    : locale === 'ka'
      ? { h: 'სთ', m: 'წთ', s: 'წმ' }
      : locale === 'es'
        ? { h: 'h', m: 'min', s: 's' }
        : { h: 'h', m: 'm', s: 's' }
  if (hours > 0) return `${hours}${suffix.h} ${String(minutes).padStart(2, '0')}${suffix.m} ${String(secs).padStart(2, '0')}${suffix.s}`
  if (minutes > 0) return `${minutes}${suffix.m} ${String(secs).padStart(2, '0')}${suffix.s}`
  return `${secs}${suffix.s}`
}

function slotTiming(slot: PersistentSlotReadModel, nowMs: number, policy: PersistentPolicyReadModel | undefined, locale: ReturnType<typeof useI18n>['locale']) {
  if (!slot.workSession) return { text: '', cue: 'normal' as const }
  const remaining = Math.max(0, Math.floor((Date.parse(slot.workSession.hardExpiresAt) - nowMs) / 1000))
  const text = formatSessionTimer(remaining, locale)
  if (!policy || policy.durationSeconds <= 0) return { text, cue: 'normal' as const }
  const elapsed = Math.max(0, policy.durationSeconds - remaining)
  const cue = elapsed >= policy.alertAfterSeconds ? 'alert' as const : elapsed >= policy.warningAfterSeconds ? 'warning' as const : 'normal' as const
  return { text, cue }
}

function cachedControl(instance: FleetInstanceView): ManagedFleetControlReadModel | undefined {
  return loadCachedFleetControlForProfile(instance.profile.instanceId, instance.profile.origin)?.control
}

function contextsFromTopology(instances: FleetInstanceView[], controls: Record<string, ManagedFleetControlReadModel | undefined>): SlotScopeContext[] {
  const membership = new Map<string, string>()
  const meshLabels = new Map<string, string>()
  for (const control of Object.values(controls)) {
    if (!control?.managed) continue
    for (const mesh of control.meshes) meshLabels.set(mesh.meshId, mesh.displayName)
    if (control.mesh) meshLabels.set(control.mesh.meshId, control.mesh.displayName)
    for (const node of control.nodes) {
      if (!node.meshId) continue
      const member = instances.find((candidate) => candidate.profile.instanceId === node.nodeId || normalizedOrigin(candidate.profile.origin) === normalizedOrigin(node.origin))
      if (member) membership.set(member.profile.instanceId, node.meshId)
    }
  }
  for (const candidate of instances) {
    const control = controls[candidate.profile.instanceId]
    if (!control?.managed) continue
    const local = control.nodes.find((node) => node.nodeId === control.nodeId || normalizedOrigin(node.origin) === normalizedOrigin(candidate.profile.origin))
    const meshId = local?.meshId ?? control.mesh?.meshId
    if (meshId) membership.set(candidate.profile.instanceId, meshId)
  }

  const contexts = new Map<string, SlotScopeContext>()
  for (const candidate of instances) {
    const meshId = membership.get(candidate.profile.instanceId)
    const ownControl = controls[candidate.profile.instanceId]
    let key: string | undefined
    let kind: SlotScopeContext['kind'] | undefined
    let label = candidate.profile.displayName
    if (meshId) {
      key = `mesh:${meshId}`
      kind = 'mesh'
      label = meshLabels.get(meshId) ?? meshId
    } else if (ownControl) {
      key = `server:${candidate.profile.instanceId}`
      kind = 'server'
    }
    if (!key || !kind) continue
    const persistent = candidate.runtime.realtime?.snapshot?.persistent
    const available = Boolean(persistent?.enabled && persistent.available)
    const error = persistent?.enabled === false ? 'disabled' : persistent?.error ?? (!available ? candidate.runtime.status : undefined)
    const existing = contexts.get(key)
    if (existing) {
      if (!existing.memberInstanceIds.includes(candidate.profile.instanceId)) existing.memberInstanceIds.push(candidate.profile.instanceId)
      existing.available ||= available
      if (!existing.error && error) existing.error = error
      const representative = instances.find((item) => item.profile.instanceId === existing.instanceId)
      if (candidate.runtime.authStatus === 'connected' && representative?.runtime.authStatus !== 'connected') existing.instanceId = candidate.profile.instanceId
    } else {
      contexts.set(key, { key, kind, label, meshId, instanceId: candidate.profile.instanceId, memberInstanceIds: [candidate.profile.instanceId], available, error })
    }
  }
  return [...contexts.values()].sort((a, b) => a.label.localeCompare(b.label))
}

function entriesForContexts(
  instances: FleetInstanceView[],
  controls: Record<string, ManagedFleetControlReadModel | undefined>,
  contexts: SlotScopeContext[],
): SlotEntry[] {
  const ownerByInstance = new Map<string, SlotScopeContext>()
  for (const context of contexts) for (const id of context.memberInstanceIds) ownerByInstance.set(id, context)
  const entries = new Map<string, SlotEntry>()
  for (const candidate of instances) {
    const owner = ownerByInstance.get(candidate.profile.instanceId)
    const persistent = candidate.runtime.realtime?.snapshot?.persistent
    if (!owner || !owner.available || !persistent?.enabled || !persistent.available) continue
    const control = controls[candidate.profile.instanceId]
    for (const slot of persistent.slots ?? []) {
      const authorityMatch = control?.nodeId === slot.authorityNodeId || candidate.profile.instanceId === slot.authorityNodeId
      const mapKey = `${owner.key}:${slot.logicalAgentId}`
      const prior = entries.get(mapKey)
      if (!prior || (authorityMatch && !prior.authorityMatch) || (authorityMatch === prior.authorityMatch && slot.slotRevision > prior.slot.slotRevision)) {
        entries.set(mapKey, { slot, instanceId: candidate.profile.instanceId, ownerKey: owner.key, authorityMatch, policy: persistent.policy, persistent })
      }
    }
  }
  return [...entries.values()]
}

export function ServerSlots({ instances, mutatePersistent, loadSlotAudit, loadFleetControl, mutateFleetControl }: { instances: FleetInstanceView[]; mutatePersistent?: PersistentMutator; loadSlotAudit?: (instanceId: string, logicalAgentId: string) => Promise<PersistentAuditReadModel[]>; loadFleetControl?: FleetControlLoader; mutateFleetControl?: FleetControlMutator }) {
  const { t, locale } = useI18n()
  const location = useLocation()
  const { instanceId: routeInstanceId = '', logicalAgentId, meshId: routeMeshId = '' } = useParams()
  const globalMode = !routeInstanceId && !routeMeshId
  const query = new URLSearchParams(location.search)
  const requestedFilter = query.get('filter') ?? sessionStorage.getItem('slots-list-filter') ?? 'all'
  const requestedSettings = query.get('settings') ?? sessionStorage.getItem('slots-settings-context') ?? ''
  const requestedOwner = query.get('owner') ?? ''
  const [globalFilter, setGlobalFilter] = useState(requestedFilter)
  const [globalContextKey, setGlobalContextKey] = useState(requestedSettings)
  const [globalControls, setGlobalControls] = useState<Record<string, ManagedFleetControlReadModel | undefined>>(() => Object.fromEntries(
    instances.map((candidate) => [candidate.profile.instanceId, cachedControl(candidate)]),
  ))
  const [controlErrors, setControlErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!globalMode || !loadFleetControl) return
    let cancelled = false
    for (const candidate of instances) {
      const id = candidate.profile.instanceId
      if (candidate.runtime.authStatus !== 'connected') continue
      void loadFleetControl(id).then((control) => {
        if (cancelled) return
        const observedAt = Date.now()
        saveCachedFleetControl(id, control, observedAt)
        propagateCachedFleetControl(control, observedAt)
        setGlobalControls((current) => ({ ...current, [id]: control }))
        setControlErrors((current) => { const next = { ...current }; delete next[id]; return next })
      }).catch((error: unknown) => {
        if (cancelled) return
        setControlErrors((current) => ({ ...current, [id]: error instanceof Error ? error.message : 'control_unavailable' }))
      })
    }
    return () => { cancelled = true }
  }, [globalMode, instances, loadFleetControl])

  const contexts = contextsFromTopology(instances, globalControls)
  const activeContext = globalMode ? (contexts.find((context) => context.key === globalContextKey) ?? contexts[0]) : undefined
  const meshId = globalMode ? activeContext?.meshId ?? '' : routeMeshId
  const routeContext = routeMeshId ? contexts.find((context) => context.key === `mesh:${routeMeshId}`) : undefined
  const effectiveRouteInstanceId = globalMode ? activeContext?.instanceId ?? '' : routeContext?.instanceId ?? routeInstanceId
  const instance = instances.find((item) => item.profile.instanceId === effectiveRouteInstanceId)
  const instanceId = instance?.profile.instanceId ?? effectiveRouteInstanceId
  const persistent = instance?.runtime.realtime?.snapshot?.persistent

  const filteredContexts = globalMode
    ? globalFilter === 'all' ? contexts : contexts.filter((context) => context.key === globalFilter)
    : []
  const globalEntries = globalMode ? entriesForContexts(instances, globalControls, filteredContexts) : []
  let scopedEntries: SlotEntry[] = []
  if (!globalMode && routeMeshId && routeContext) {
    scopedEntries = entriesForContexts(instances, globalControls, [routeContext])
  } else if (!globalMode && instance && persistent?.enabled && persistent.available) {
    scopedEntries = (persistent.slots ?? []).map((slot) => ({
      slot,
      instanceId,
      ownerKey: `server:${instanceId}`,
      authorityMatch: true,
      policy: persistent.policy,
      persistent,
    }))
  }
  const slotEntries = globalMode ? globalEntries : scopedEntries
  const slots = slotEntries.map(({ slot }) => slot).sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.slotRevision - a.slotRevision)
  const selectedEntry = logicalAgentId ? slotEntries.find((entry) => entry.slot.logicalAgentId === logicalAgentId && (!requestedOwner || entry.ownerKey === requestedOwner)) : undefined
  const selected = selectedEntry?.slot
  const selectedPersistent = selectedEntry?.persistent
  const selectedPolicy = selectedEntry?.policy
  const selectedInstanceId = selectedEntry?.instanceId ?? instanceId
  const entryForSlot = (slot: PersistentSlotReadModel): SlotEntry | undefined => slotEntries.find((entry) => entry.slot === slot) ?? slotEntries.find((entry) => entry.slot.logicalAgentId === slot.logicalAgentId)
  const instanceIdForSlot = (slot: PersistentSlotReadModel): string => entryForSlot(slot)?.instanceId ?? instanceId
  const scrollStorageKey = `slots-scroll:${globalFilter}`
  const rememberGlobalScroll = () => {
    if (!globalMode) return
    sessionStorage.setItem('slots-list-filter', globalFilter)
    if (activeContext) sessionStorage.setItem('slots-settings-context', activeContext.key)
    sessionStorage.setItem(scrollStorageKey, String(window.scrollY))
  }
  useEffect(() => {
    if (!globalMode || logicalAgentId) return
    sessionStorage.setItem('slots-list-filter', globalFilter)
    const top = Number(sessionStorage.getItem(scrollStorageKey) ?? '0')
    if (Number.isFinite(top) && top > 0) requestAnimationFrame(() => window.scrollTo({ top }))
  }, [globalMode, globalFilter, logicalAgentId, scrollStorageKey])
  useEffect(() => {
    if (!globalMode || !activeContext) return
    sessionStorage.setItem('slots-settings-context', activeContext.key)
  }, [activeContext, globalMode])
  const [clockOrigin] = useState(() => Date.now()); const [clock, setClock] = useState(() => Date.now()); const [anchor, setAnchor] = useState(() => ({ server: '', serverMs: Date.now(), localMs: Date.now() })); const [busy, setBusy] = useState(''); const [message, setMessage] = useState(''); const [createName, setCreateName] = useState(''); const [reassignTarget, setReassignTarget] = useState('')
  const [accessCodeRevision, setAccessCodeRevision] = useState(0)
  const [createdAccess, setCreatedAccess] = useState<{ logicalAgentId: string; publicName: string; displayName: string; code: string; generation: number } | null>(null)
  const [policyDraft, setPolicyDraft] = useState<TimingDraft | null>(null)
  const [loadedAudit, setLoadedAudit] = useState<{ key: string; values?: PersistentAuditReadModel[]; error?: string }>({ key: '' })
  const [fleetControl, setFleetControl] = useState<ManagedFleetControlReadModel | null>(() => loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)?.control ?? null)
  const [, setFleetControlFreshness] = useState<FleetControlFreshness>(() => loadCachedFleetControlForProfile(instanceId, instance?.profile.origin) ? 'stale' : 'unknown')
  const [confirmAction, setConfirmAction] = useState<{ kind: 'rotate' | 'delete'; logicalAgentId: string } | null>(null)
  useEffect(() => {
    let cancelled = false
    const handle = window.setTimeout(() => {
      const cached = loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)
      setFleetControl(cached?.control ?? null)
      setFleetControlFreshness(cached ? 'stale' : 'unknown')
      if (!loadFleetControl || instance?.runtime.authStatus !== 'connected') return
      void loadFleetControl(instanceId)
        .then((control) => {
          if (cancelled) return
          const observedAt = Date.now()
          saveCachedFleetControl(instanceId, control, observedAt)
          propagateCachedFleetControl(control, observedAt)
          setFleetControl(control)
          if (globalMode) setGlobalControls((current) => ({ ...current, [instanceId]: control }))
          setFleetControlFreshness('fresh')
        })
        .catch(() => {
          if (cancelled) return
          setFleetControl((current) => {
            setFleetControlFreshness(current ? 'stale' : 'unknown')
            return current
          })
        })
    }, 0)
    return () => { cancelled = true; window.clearTimeout(handle) }
  }, [globalMode, instance?.profile.origin, instance?.runtime.authStatus, instanceId, loadFleetControl])

  const selectedLogicalAgentId = selected?.logicalAgentId
  useEffect(() => {
    if (!selectedLogicalAgentId || !loadSlotAudit) return
    const key = selectedInstanceId + ':' + selectedLogicalAgentId
    let cancelled = false
    void loadSlotAudit(selectedInstanceId, selectedLogicalAgentId)
      .then((values) => { if (!cancelled) setLoadedAudit({ key, values }) })
      .catch((error: unknown) => {
        if (!cancelled) setLoadedAudit({ key, error: error instanceof Error ? error.message : 'slot_audit_unavailable' })
      })
    return () => { cancelled = true }
  }, [loadSlotAudit, selectedInstanceId, selectedLogicalAgentId])
  const selectedAuditKey = selected ? selectedInstanceId + ':' + selected.logicalAgentId : ''
  const selectedAudit = loadedAudit.key === selectedAuditKey && loadedAudit.values
    ? loadedAudit.values
    : (selected?.audit ?? [])
  const auditError = loadedAudit.key === selectedAuditKey ? loadedAudit.error : undefined
  const serverNow = persistent?.serverNow ?? slots[0]?.serverNow ?? ''
  useEffect(() => {
    if (!serverNow || serverNow === anchor.server) return
    const timer = window.setTimeout(() => {
      setAnchor({ server: serverNow, serverMs: Date.parse(serverNow), localMs: Date.now() })
    }, 0)
    return () => window.clearTimeout(timer)
  }, [anchor.server, serverNow])
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 1000); return () => window.clearInterval(timer) }, [])
  const authorityNowMs = anchor.server ? anchor.serverMs + (clock - anchor.localMs) : clock
  const managedPolicy = fleetControl?.managed ? fleetControl.policy : undefined
  const managedAuthorityNode = fleetControl?.managed
    ? fleetControl.nodes.find((node) => node.nodeId === fleetControl.controlNodeId)
    : undefined
  const managedAuthorityInstance = fleetControl?.managed
    ? instances.find((candidate) => {
        if (candidate.profile.instanceId === fleetControl.controlNodeId) return true
        const cached = loadCachedFleetControlForProfile(candidate.profile.instanceId, candidate.profile.origin)?.control
        return cached?.nodeId === fleetControl.controlNodeId
          || Boolean(managedAuthorityNode?.origin && normalizedOrigin(candidate.profile.origin) === normalizedOrigin(managedAuthorityNode.origin))
      })
    : undefined
  const managedAuthorityInstanceId = managedAuthorityInstance?.profile.instanceId
  const standaloneConfirmed = fleetControl ? !fleetControl.managed : !loadFleetControl
  const effectivePolicy = managedPolicy ?? (standaloneConfirmed ? persistent?.policy : undefined)
  const policyContextKey = globalMode ? activeContext?.key ?? '' : meshId ? `mesh:${meshId}` : `server:${instanceId}`
  const policyKey = effectivePolicy ? [policyContextKey, effectivePolicy.durationSeconds, effectivePolicy.warningAfterSeconds, effectivePolicy.alertAfterSeconds, effectivePolicy.rearmAfterSeconds, effectivePolicy.legacyAdmissionEnabled ? 1 : 0].join(':') : ''
  const draft: TimingDraft = policyDraft?.key === policyKey ? policyDraft : { key: policyKey, duration: durationValue(effectivePolicy?.durationSeconds ?? 0), warning: durationValue(effectivePolicy?.warningAfterSeconds ?? 0), alert: durationValue(effectivePolicy?.alertAfterSeconds ?? 0), rearm: durationValue(effectivePolicy?.rearmAfterSeconds ?? 0), legacyAdmissionEnabled: effectivePolicy?.legacyAdmissionEnabled ?? false }
  // Read freshness and write reachability are separate planes. A cached projection must not
  // suppress an authenticated direct-authority mutation attempt.
  const canMutate = Boolean(mutatePersistent && persistent?.enabled && instance?.runtime.authStatus === 'connected')
  const canMutateSlot = (slot: PersistentSlotReadModel) => {
    const target = instances.find((candidate) => candidate.profile.instanceId === instanceIdForSlot(slot))
    return Boolean(mutatePersistent && target?.runtime.authStatus === 'connected')
  }
  const policyCanMutate = managedPolicy
    ? Boolean(mutateFleetControl && managedAuthorityInstanceId && managedAuthorityInstance?.runtime.authStatus === 'connected')
    : standaloneConfirmed ? canMutate : false
  const mutationAt = async (targetInstanceId: string, key: string, path: string, body: Record<string, unknown>) => {
    const target = instances.find((candidate) => candidate.profile.instanceId === targetInstanceId)
    if (!mutatePersistent || target?.runtime.authStatus !== 'connected') { setMessage(t('slots.liveRequired')); return false }
    setBusy(key); setMessage('')
    try {
      const result = await mutatePersistent(targetInstanceId, path, body)
      if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return false }
      setMessage(t('slots.mutationApplied'))
      return true
    } catch (error) {
      setMessage(error instanceof Error ? error.message : t('slots.mutationFailed'))
      return false
    } finally { setBusy('') }
  }
  const mutation = (key: string, path: string, body: Record<string, unknown>) => mutationAt(instanceId, key, path, body)
  const managedPolicyMutation = async (key: string, path: string, body: Record<string, unknown>) => {
    if (!mutateFleetControl || !managedAuthorityInstanceId || managedAuthorityInstance?.runtime.authStatus !== 'connected') { setMessage(t('slots.liveRequired')); return false }
    setBusy(key); setMessage('')
    try {
      const result = await mutateFleetControl(managedAuthorityInstanceId, path, body)
      if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return false }
      if (result.control) {
        const observedAt = Date.now()
        saveCachedFleetControl(managedAuthorityInstanceId, result.control, observedAt)
        saveCachedFleetControl(instanceId, { ...result.control, nodeId: fleetControl?.nodeId ?? result.control.nodeId, mesh: fleetControl?.mesh }, observedAt)
        propagateCachedFleetControl(result.control, observedAt)
        setFleetControl(result.control)
        setFleetControlFreshness('fresh')
      }
      setMessage(t('slots.mutationApplied'))
      return true
    } catch (error) { setMessage(error instanceof Error ? error.message : t('slots.mutationFailed')); return false }
    finally { setBusy('') }
  }
  const mutateSlot = (slot: PersistentSlotReadModel, action: 'play' | 'suspend' | 'delete') => mutationAt(instanceIdForSlot(slot), `${slot.logicalAgentId}:${action}`, `/actions/persistent/slots/${action}`, { logical_agent_id: slot.logicalAgentId, expected_revision: slot.slotRevision, idempotency_key: idempotencyKey() })
  const timingPolicyLocked = slots.some((slot) => ['armed', 'active', 'stopping'].includes(slot.state))
  const saveTimingPolicy = async () => {
    const durationSeconds = durationSecondsValue(draft.duration); const warningAfterSeconds = durationSecondsValue(draft.warning); const alertAfterSeconds = durationSecondsValue(draft.alert); const rearmAfterSeconds = durationSecondsValue(draft.rearm)
    if (durationSeconds === null || warningAfterSeconds === null || alertAfterSeconds === null || rearmAfterSeconds === null || !(0 < warningAfterSeconds && warningAfterSeconds < alertAfterSeconds && alertAfterSeconds < durationSeconds) || rearmAfterSeconds <= 0) {
      setMessage(t('slots.policyInvalid')); return
    }
    if (managedPolicy) {
      await managedPolicyMutation('policy', '/actions/fleet/control/policy', {
        duration_seconds: durationSeconds,
        warning_after_seconds: warningAfterSeconds,
        alert_after_seconds: alertAfterSeconds,
        rearm_after_seconds: rearmAfterSeconds,
        legacy_admission_enabled: draft.legacyAdmissionEnabled,
        expected_revision: managedPolicy.revision,
      })
      return
    }
    await mutation('policy', '/actions/persistent/policy', { duration_seconds: durationSeconds, warning_after_seconds: warningAfterSeconds, alert_after_seconds: alertAfterSeconds, rearm_after_seconds: rearmAfterSeconds, legacy_admission_enabled: draft.legacyAdmissionEnabled })
  }
  const resetPolicy = async () => {
    if (managedPolicy) {
      await managedPolicyMutation('policy-reset', '/actions/fleet/control/policy/reset', { expected_revision: managedPolicy.revision })
      return
    }
    if (!standaloneConfirmed) return
    await mutation('policy-reset', '/actions/persistent/policy', { duration_seconds: 23 * 60, warning_after_seconds: 20 * 60, alert_after_seconds: 22 * 60, rearm_after_seconds: 3 * 60, legacy_admission_enabled: false })
  }
  const activeSession = selected?.workSession
  const accessReady = Boolean(selected && ((selected.access?.accessGeneration ?? 0) > 0 || loadAccessCode(selected.logicalAgentId, selected.access?.accessGeneration ?? 0)))
  const otherSlots = slots.filter(
    (slot) => slot.logicalAgentId !== selected?.logicalAgentId && slot.state !== 'deleted',
  )
  if (!instance) return globalMode
    ? <FeedbackState variant="empty" title={t('slots.empty')} />
    : meshId
      ? <FeedbackState variant="partial" title={t('title.mesh')} detail={t('slots.liveRequired')} />
      : <Navigate to="/" replace />
  const writeClipboard = async (code: string): Promise<boolean> => {
    try { await navigator.clipboard.writeText(code); return true } catch { return false }
  }
  const createSlot = async (displayName: string) => {
    if (!canMutate || !mutatePersistent) { setMessage(t('slots.liveRequired')); return }
    setBusy('create'); setMessage(''); setCreatedAccess(null)
    try {
      const result = await mutatePersistent(instanceId, '/actions/persistent/slots/create', { display_name: displayName })
      if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return }
      const slotPayload = result.payload.slot
      const accessPayload = result.payload.access
      const slotRecord = slotPayload && typeof slotPayload === 'object' && !Array.isArray(slotPayload)
        ? slotPayload as Record<string, unknown>
        : undefined
      const access = accessPayload && typeof accessPayload === 'object' && !Array.isArray(accessPayload)
        ? accessPayload as Record<string, unknown>
        : undefined
      const logicalAgentId = typeof slotRecord?.logical_agent_id === 'string' ? slotRecord.logical_agent_id : ''
      const generation = Number.isInteger(access?.access_generation) ? Number(access?.access_generation) : 0
      const code = typeof access?.access_code === 'string' ? access.access_code : ''
      const publicName = typeof access?.public_name === 'string' ? access.public_name : displayName
      if (logicalAgentId && /^[0-9]{4}$/.test(code) && generation > 0) {
        saveAccessCode(logicalAgentId, { code, publicName, generation })
        setAccessCodeRevision((value) => value + 1)
        setCreatedAccess({ logicalAgentId, publicName, displayName, code, generation })
        setCreateName('')
        const copied = await writeClipboard(code)
        setMessage(copied ? t('slots.mutationApplied') + ' ' + t('slots.accessCodeCopied') : t('slots.mutationApplied') + ' ' + t('slots.copyFailed'))
        return
      }
      setMessage(t('slots.accessCodeUnavailable'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : t('slots.mutationFailed'))
    } finally {
      setBusy('')
    }
  }
  const mutateAccess = async (slot: PersistentSlotReadModel, action: 'setup' | 'rotate') => {
    if (!canMutateSlot(slot) || !mutatePersistent) { setMessage(t('slots.liveRequired')); return }
    const path = action === 'setup'
      ? '/actions/persistent/slots/migrate-access'
      : '/actions/persistent/slots/rotate-access-code'
    setBusy(`${slot.logicalAgentId}:access:${action}`)
    setMessage('')
    try {
      const result = await mutatePersistent(instanceIdForSlot(slot), path, { logical_agent_id: slot.logicalAgentId })
      if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return }
      const raw = result.payload.access
      const access = raw && typeof raw === 'object' && !Array.isArray(raw)
        ? raw as Record<string, unknown>
        : undefined
      const generation = Number.isInteger(access?.access_generation)
        ? Number(access?.access_generation)
        : 0
      const code = typeof access?.access_code === 'string' ? access.access_code : ''
      const publicName = typeof access?.public_name === 'string' ? access.public_name : undefined
      if (/^[0-9]{4}$/.test(code) && generation > 0) {
        saveAccessCode(slot.logicalAgentId, { code, publicName, generation })
        setAccessCodeRevision((value) => value + 1)
        if (createdAccess?.logicalAgentId === slot.logicalAgentId) setCreatedAccess(null)
        const copied = await writeClipboard(code)
        setMessage(copied ? t('slots.mutationApplied') + ' ' + t('slots.accessCodeCopied') : t('slots.mutationApplied') + ' ' + t('slots.copyFailed'))
        return
      }
      setMessage(
        action === 'setup' && generation > 0
          ? t('slots.accessAlreadyExists')
          : t('slots.accessCodeUnavailable'),
      )
    } catch (error) {
      setMessage(error instanceof Error ? error.message : t('slots.mutationFailed'))
    } finally {
      setBusy('')
    }
  }
  const copyAccessCode = async (code: string) => {
    setMessage(await writeClipboard(code) ? t('slots.accessCodeCopied') : t('slots.copyFailed'))
  }
  const storedAccessCode = (slot: PersistentSlotReadModel) => {
    void accessCodeRevision
    return loadAccessCode(slot.logicalAgentId, slot.access?.accessGeneration ?? 0)
  }
  const slotPublicName = (slot: PersistentSlotReadModel): string => {
    const publicName = slot.access?.publicName?.trim() ?? ''
    return publicName && publicName !== slot.logicalAgentId ? publicName : t('slots.identityUnavailable')
  }
  const slotDisplayName = (slot: PersistentSlotReadModel): string => {
    const displayName = slot.displayName.trim()
    return displayName && displayName !== slot.logicalAgentId ? displayName : ''
  }
  const slotIdentity = (slot: PersistentSlotReadModel): string => {
    const publicName = slotPublicName(slot)
    const displayName = slotDisplayName(slot)
    return displayName && displayName !== publicName ? publicName + ' · ' + displayName : publicName
  }
  const slotStateLabel = (slot: PersistentSlotReadModel): string => {
    if (slot.state === 'suspended' && slot.rearm) {
      const remaining = Math.max(0, Math.floor((Date.parse(slot.rearm.rearmAt) - authorityNowMs) / 1000))
      return t('slots.rearmAfterSeconds') + ': ' + formatDuration(remaining, locale)
    }
    return localizedSlotState(t, slot.state)
  }
  const confirmSlot = confirmAction ? slots.find((slot) => slot.logicalAgentId === confirmAction.logicalAgentId) : undefined
  const performConfirmedAction = async () => {
    const action = confirmAction
    const slot = confirmSlot
    if (!action || !slot) { setConfirmAction(null); return }
    setConfirmAction(null)
    if (action.kind === 'rotate') {
      await mutateAccess(slot, 'rotate')
      return
    }
    const ok = await mutateSlot(slot, 'delete')
    if (ok) {
      clearAccessCode(slot.logicalAgentId)
      setAccessCodeRevision((value) => value + 1)
      if (createdAccess?.logicalAgentId === slot.logicalAgentId) setCreatedAccess(null)
    }
  }
  const slotHref = (slot: PersistentSlotReadModel) => {
    if (!globalMode) return meshId ? meshPersistentSlotRoute(meshId, slot.logicalAgentId) : slotRoute(instanceIdForSlot(slot), slot.logicalAgentId)
    const entry = entryForSlot(slot)
    const params = new URLSearchParams()
    if (globalFilter !== 'all') params.set('filter', globalFilter)
    if (activeContext) params.set('settings', activeContext.key)
    if (entry) params.set('owner', entry.ownerKey)
    const search = params.toString()
    return `/slots/${encodeURIComponent(slot.logicalAgentId)}${search ? `?${search}` : ''}`
  }
  return <section className="stack compact-operational-surface slots-surface" aria-label={t('nav.slots')}>
    {globalMode && !selected ? <div className="slots-global-filter surface-section"><label className="ui-field"><span>{t('slots.filter')}</span><select value={globalFilter} onChange={(event) => setGlobalFilter(event.target.value)}><option value="all">{t('slots.all')}</option>{contexts.map((context) => <option key={context.key} value={context.key}>{context.label}</option>)}</select></label></div> : null}
    <div className="operational-status-slot" aria-live="polite">{!globalMode && instance.runtime.status !== 'live' ? <div className="attention-strip" role="status">{t('slots.cachedReadOnly')} {localizedRuntimeState(t, instance.runtime.status)}.</div> : null}</div>
    {!globalMode && persistent && !persistent.enabled && <FeedbackState variant="empty" title={t('slots.disabled')} />}
    {!globalMode && persistent?.enabled && !persistent.available && <FeedbackState variant="error" title={t('slots.unavailable')} detail={persistent.error ?? undefined} />}
    {globalMode && !selected ? <div className="slot-context-statuses">{filteredContexts.filter((context) => !context.available).map((context) => <div className="attention-strip" role="status" key={context.key}><strong>{context.label}</strong><span>{t('slots.unavailable')}{context.error ? ` · ${context.error}` : ''}</span></div>)}{instances.filter((candidate) => !globalControls[candidate.profile.instanceId]).map((candidate) => <div className="attention-strip" role="status" key={'unresolved:' + candidate.profile.instanceId}><strong>{candidate.profile.displayName}</strong><span>{t('slots.policyScopeUnknown')} · {controlErrors[candidate.profile.instanceId] ?? localizedRuntimeState(t, candidate.runtime.status)}</span></div>)}</div> : null}
    {(message || (persistent?.enabled && loadFleetControl && !fleetControl)) ? <div className="floating-status-stack" aria-live="polite">{message ? <div className="attention-strip" role="status"><span>{message}</span></div> : null}{persistent?.enabled && loadFleetControl && !fleetControl ? <div className="attention-strip" role="status">{t('slots.policyScopeUnknown')}</div> : null}</div> : null}
    {confirmAction && confirmSlot ? <div className="floating-status-stack confirmation-surface"><div className="panel" role="dialog" aria-modal="true" aria-label={confirmAction.kind === 'rotate' ? t('slots.rotateAccessCode') : t('slots.delete')}><p><strong>{slotIdentity(confirmSlot)}</strong></p><p>{confirmAction.kind === 'rotate' ? t('slots.rotateAccessConfirm') : t('slots.deleteConfirm')}</p><div className="server-actions"><button type="button" className="secondary-action" onClick={() => setConfirmAction(null)}>{t('slots.confirmCancel')}</button><button type="button" className={confirmAction.kind === 'delete' ? 'destructive-action' : 'primary-action'} onClick={() => void performConfirmedAction()}>{confirmAction.kind === 'delete' ? t('slots.delete') : t('slots.rotateAccessCode')}</button></div></div></div> : null}
    {!selected && persistent?.enabled && effectivePolicy && (persistent.policy.policyControlSupported || managedPolicy) && <article className="surface-section policy-controls"><div className="section-heading"><h3>{t('slots.policyControls')}</h3></div>{globalMode ? <label className="ui-field slots-context-select"><span>{t('slots.settingsContext')}</span><select value={activeContext?.key ?? ''} onChange={(event) => setGlobalContextKey(event.target.value)}>{contexts.map((context) => <option key={context.key} value={context.key}>{context.label}</option>)}</select></label> : null}<div className="policy-controls-grid"><label className="ui-field"><span>{t('slots.durationSeconds')}</span><span className="duration-control"><input aria-label={t('slots.durationSeconds')} inputMode="numeric" type="number" min="0" step="1" value={draft.duration.amount} onChange={(event) => setPolicyDraft({ ...draft, duration: { ...draft.duration, amount: event.target.value } })} disabled={!policyCanMutate || Boolean(busy)} /><select aria-label={t('slots.durationSeconds') + ' · ' + t('slots.unit')} value={draft.duration.unit} onChange={(event) => setPolicyDraft({ ...draft, duration: { ...draft.duration, unit: event.target.value as DurationUnit } })} disabled={!policyCanMutate || Boolean(busy)}><option value="seconds">{t('slots.unit.seconds')}</option><option value="minutes">{t('slots.unit.minutes')}</option><option value="hours">{t('slots.unit.hours')}</option></select></span></label><label className="ui-field"><span>{t('slots.warningAfterSeconds')}</span><span className="duration-control"><input aria-label={t('slots.warningAfterSeconds')} inputMode="numeric" type="number" min="0" step="1" value={draft.warning.amount} onChange={(event) => setPolicyDraft({ ...draft, warning: { ...draft.warning, amount: event.target.value } })} disabled={!policyCanMutate || Boolean(busy)} /><select aria-label={t('slots.warningAfterSeconds') + ' · ' + t('slots.unit')} value={draft.warning.unit} onChange={(event) => setPolicyDraft({ ...draft, warning: { ...draft.warning, unit: event.target.value as DurationUnit } })} disabled={!policyCanMutate || Boolean(busy)}><option value="seconds">{t('slots.unit.seconds')}</option><option value="minutes">{t('slots.unit.minutes')}</option><option value="hours">{t('slots.unit.hours')}</option></select></span></label><label className="ui-field"><span>{t('slots.alertAfterSeconds')}</span><span className="duration-control"><input aria-label={t('slots.alertAfterSeconds')} inputMode="numeric" type="number" min="0" step="1" value={draft.alert.amount} onChange={(event) => setPolicyDraft({ ...draft, alert: { ...draft.alert, amount: event.target.value } })} disabled={!policyCanMutate || Boolean(busy)} /><select aria-label={t('slots.alertAfterSeconds') + ' · ' + t('slots.unit')} value={draft.alert.unit} onChange={(event) => setPolicyDraft({ ...draft, alert: { ...draft.alert, unit: event.target.value as DurationUnit } })} disabled={!policyCanMutate || Boolean(busy)}><option value="seconds">{t('slots.unit.seconds')}</option><option value="minutes">{t('slots.unit.minutes')}</option><option value="hours">{t('slots.unit.hours')}</option></select></span></label><label className="ui-field"><span>{t('slots.rearmAfterSeconds')}</span><span className="duration-control"><input aria-label={t('slots.rearmAfterSeconds')} inputMode="numeric" type="number" min="0" step="1" value={draft.rearm.amount} onChange={(event) => setPolicyDraft({ ...draft, rearm: { ...draft.rearm, amount: event.target.value } })} disabled={!policyCanMutate || Boolean(busy)} /><select aria-label={t('slots.rearmAfterSeconds') + ' · ' + t('slots.unit')} value={draft.rearm.unit} onChange={(event) => setPolicyDraft({ ...draft, rearm: { ...draft.rearm, unit: event.target.value as DurationUnit } })} disabled={!policyCanMutate || Boolean(busy)}><option value="seconds">{t('slots.unit.seconds')}</option><option value="minutes">{t('slots.unit.minutes')}</option><option value="hours">{t('slots.unit.hours')}</option></select></span></label></div><label className="policy-toggle"><input aria-label={t('slots.legacyToggle')} type="checkbox" checked={draft.legacyAdmissionEnabled} disabled={!policyCanMutate || Boolean(busy)} onChange={(event) => setPolicyDraft({ ...draft, legacyAdmissionEnabled: event.target.checked })} /><span><strong>{t('slots.legacyToggle')}</strong></span></label><div className="server-actions policy-actions"><button type="button" className="primary-action" disabled={!policyCanMutate || Boolean(busy)} onClick={() => void saveTimingPolicy()}>{t('slots.savePolicy')}</button><button type="button" className="secondary-action" disabled={!policyCanMutate || Boolean(busy)} onClick={() => void resetPolicy()}>{t('slots.resetPolicy')}</button></div>{!managedPolicy && timingPolicyLocked && <p className="muted">{t('slots.policyLocked')}</p>}</article>}
    {!selected && persistent?.enabled && <form className="slot-create surface-section" onSubmit={(event) => { event.preventDefault(); void createSlot(createName.trim()) }}><h3>{t('slots.createSection')}</h3><div className="slot-create-row"><label className="ui-field"><span>{t('slots.createName')}</span><input value={createName} onChange={(event) => setCreateName(event.target.value)} maxLength={120} /></label><button type="submit" className="primary-action" disabled={!canMutate || busy === 'create'}>{t('slots.create')}</button></div>{createdAccess ? <div className="slot-access-code created-access-row" role="status"><span>{createdAccess.publicName}{createdAccess.displayName && createdAccess.displayName !== createdAccess.publicName ? ` · ${createdAccess.displayName}` : ''}</span><button type="button" className="access-code-copy" aria-label={t('slots.copyAccessCode') + ' — ' + createdAccess.publicName} onClick={() => void copyAccessCode(createdAccess.code)}><code>{createdAccess.code}</code><span aria-hidden="true">▣</span></button></div> : null}</form>}
    {selected && selectedPersistent && <article className="panel slot-detail" aria-label={t('title.slotDetail')}><div className="section-heading"><div><p className="eyebrow">{t('slots.accessPublicName')}</p><h3>{slotIdentity(selected)}</h3></div><span className="chip">{slotStateLabel(selected)}</span></div><div className="slot-access-code"><span>{t('slots.accessCodeTitle')}</span>{storedAccessCode(selected) ? <><code>{storedAccessCode(selected)!.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + slotIdentity(selected)} onClick={() => void copyAccessCode(storedAccessCode(selected)!.code)}>{t('slots.copyAccessCode')}</button></> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</div><details className="technical-details"><summary>{t('slots.technicalDetails')}</summary><dl className="slot-details-grid"><div><dt>{t('slots.logicalAgentId')}</dt><dd><code>{selected.logicalAgentId}</code></dd></div><div><dt>{t('slots.rawState')}</dt><dd><code>{selected.state}</code></dd></div><div><dt>{t('slots.legacySelector')}</dt><dd><code>{selected.selector}</code></dd></div><div><dt>{t('slots.selectorGeneration')}</dt><dd>{selected.selectorGeneration}</dd></div><div><dt>{t('slots.authGeneration')}</dt><dd>{selected.authGeneration}</dd></div><div><dt>{t('slots.accessPublicName')}</dt><dd>{selected.access?.publicName ?? storedAccessCode(selected)?.publicName ?? slotIdentity(selected)}</dd></div><div><dt>{t('slots.accessGeneration')}</dt><dd>{selected.access?.accessGeneration ?? 0}</dd></div><div><dt>{t('slots.accessCodeTitle')}</dt><dd>{storedAccessCode(selected) ? <span className="access-code-inline"><code>{storedAccessCode(selected)?.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + slotIdentity(selected)} onClick={() => void copyAccessCode(storedAccessCode(selected)!.code)}>{t('slots.copyAccessCode')}</button></span> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</dd></div><div><dt>{t('slots.authority')}</dt><dd>{selected.authorityNodeId} · e{selected.authorityEpoch}</dd></div><div><dt>{t('slots.revision')}</dt><dd>{selected.slotRevision}</dd></div><div><dt>{t('slots.session')}</dt><dd>{activeSession ? <code>{activeSession.workSessionId}</code> : t('slots.noSession')}</dd></div><div><dt>{t('slots.sessionEpoch')}</dt><dd>{activeSession?.sessionEpoch ?? '—'}</dd></div><div><dt>{t('slots.hardExpiresAt')}</dt><dd>{activeSession?.hardExpiresAt ?? '—'}</dd></div><div><dt>{t('slots.policy')}</dt><dd>{selectedPolicy?.projected ? t('common.unavailable') : selectedPolicy ? <>{t('slots.durationSeconds')} {formatDuration(selectedPolicy.durationSeconds, locale)} · {t('slots.warningAfterSeconds')} {formatDuration(selectedPolicy.warningAfterSeconds, locale)} · {t('slots.alertAfterSeconds')} {formatDuration(selectedPolicy.alertAfterSeconds, locale)} · {t('slots.rearmAfterSeconds')} {formatDuration(selectedPolicy.rearmAfterSeconds, locale)}</> : t('common.unavailable')}</dd></div><div><dt>{t('slots.manualRearm')}</dt><dd>{selectedPolicy?.projected ? t('common.unavailable') : selectedPolicy ? selectedPolicy.manualRearm ? t('slots.yes') : t('slots.no') : t('common.unavailable')}</dd></div><div><dt>{t('slots.admissionMode')}</dt><dd>{selectedPolicy?.projected ? t('common.unavailable') : selectedPolicy?.admissionMode ?? t('common.unavailable')}</dd></div><div><dt>{t('slots.legacyAdmission')}</dt><dd>{selectedPolicy?.projected ? t('common.unavailable') : selectedPolicy ? selectedPolicy.legacyAdmissionEnabled ? t('slots.yes') : t('slots.no') : t('common.unavailable')}</dd></div><div><dt>{t('slots.createdAt')}</dt><dd>{selected.createdAt}</dd></div><div><dt>{t('slots.updatedAt')}</dt><dd>{selected.updatedAt}</dd></div><div><dt>{t('slots.fleet')}</dt><dd>{selected.attachments.length ? selected.attachments.map((attachment) => <div key={attachment.nodeAttachmentId}><span>{attachment.nodeInstanceId}</span> · <code>{attachment.nodeAttachmentId}</code></div>) : t('slots.noAttachments')}</dd></div><div><dt>{t('slots.audit')}</dt><dd>{selectedAudit.length ? selectedAudit.map((entry) => <div key={entry.id}><code>#{entry.id}</code> · <code>{entry.principalId}</code> · <code>{entry.eventType}</code></div>) : t('slots.noAudit')}</dd></div></dl></details><div className="server-actions">{!accessReady && <button type="button" disabled={!canMutateSlot(selected) || Boolean(busy)} onClick={() => void mutateAccess(selected, 'setup')}>{t('slots.setupAccessCode')}</button>}{accessReady && <button type="button" className="secondary-action" disabled={!canMutateSlot(selected) || Boolean(busy)} onClick={() => setConfirmAction({ kind: 'rotate', logicalAgentId: selected.logicalAgentId })}>{t('slots.rotateAccessCode')}</button>}</div>
      <div className="slot-subsection"><h3>{t('slots.claims')}</h3>{selected.claims.length ? selected.claims.map((claim) => <div className="slot-claim" key={`${claim.namespace}/${claim.taskId}`}><Link className="text-link" to={taskRoute(selectedInstanceId, claim.namespace, claim.taskId)}>{claim.namespace}/{claim.taskId}</Link><span>{claim.priority} · {localizedClaimState(t, claim.state)}</span>{activeSession && <div className="server-actions"><button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutationAt(selectedInstanceId, `release:${claim.namespace}/${claim.taskId}`, '/actions/persistent/claims/release', { namespace: claim.namespace, task_id: claim.taskId, logical_agent_id: selected.logicalAgentId, work_session_id: activeSession.workSessionId, session_epoch: activeSession.sessionEpoch })}>{t('slots.releaseClaim')}</button>{otherSlots.length > 0 && <><select aria-label={t('slots.reassignTarget')} value={reassignTarget} onChange={(event) => setReassignTarget(event.target.value)}><option value="">{t('slots.chooseTarget')}</option>{otherSlots.map((slot) => <option key={slot.logicalAgentId} value={slot.logicalAgentId}>{slotIdentity(slot)}</option>)}</select><button type="button" disabled={!canMutate || !reassignTarget || Boolean(busy)} onClick={() => void mutationAt(selectedInstanceId, `reassign:${claim.namespace}/${claim.taskId}`, '/actions/persistent/claims/reassign', { namespace: claim.namespace, task_id: claim.taskId, logical_agent_id: selected.logicalAgentId, to_logical_agent_id: reassignTarget, work_session_id: activeSession.workSessionId, session_epoch: activeSession.sessionEpoch, expected_revision: selected.slotRevision, idempotency_key: idempotencyKey() })}>{t('slots.reassignClaim')}</button></>}</div>}</div>) : <FeedbackState variant="empty" title={t('slots.noClaims')} />}</div>
      <div className="slot-subsection"><h3>{t('slots.fleet')}</h3>{selected.attachments.length ? selected.attachments.map((attachment) => <p key={attachment.nodeAttachmentId}>{attachment.nodeInstanceId}</p>) : <FeedbackState variant="empty" title={t('slots.noAttachments')} />}</div><div className="slot-subsection"><h3>{t('slots.audit')}</h3>{selectedAudit.length ? <ul className="slot-audit">{selectedAudit.map((entry) => <li key={entry.id}><strong>{localizedAuditEvent(t, entry.eventType)}</strong><span>{entry.createdAt}</span></li>)}</ul> : <p className="muted">{t('slots.noAudit')}</p>}{auditError ? <p className="muted" role="status">{auditError}</p> : null}</div></article>}
    {!selected && <div className="slot-grid">{slots.map((slot) => {
      const entry = entryForSlot(slot)
      const baseNow = Date.parse(slot.serverNow || entry?.persistent.serverNow || '')
      const slotNowMs = Number.isFinite(baseNow) ? baseNow + (clock - clockOrigin) : clock
      const timing = slotTiming(slot, slotNowMs, entry?.policy, locale)
      const saved = storedAccessCode(slot)
      const slotAccessReady = (slot.access?.accessGeneration ?? 0) > 0 || saved !== null
      const displayName = slotDisplayName(slot)
      return <article className={'panel slot-card slot-cue-' + timing.cue} key={(entry?.ownerKey ?? '') + ':' + slot.logicalAgentId}>
        <div className="slot-card-primary">
          <Link className="text-link slot-card-identity" to={slotHref(slot)} onClick={rememberGlobalScroll}><strong>{slotPublicName(slot)}</strong>{displayName && displayName !== slotPublicName(slot) ? <span> · {displayName}</span> : null}</Link>
          <span className={'slot-timer slot-timer-' + timing.cue} role={timing.cue === 'normal' ? undefined : 'status'}>{slot.workSession ? timing.text : '\u00a0'}</span>
          <span className="chip">{slotStateLabel(slot)}</span>
        </div>
        <div className="slot-card-secondary">
          <span className="slot-code-cell">{saved ? <button type="button" className="access-code-copy" aria-label={t('slots.copyAccessCode') + ' — ' + slotIdentity(slot)} onClick={() => void copyAccessCode(saved.code)}><code>{saved.code}</code><span aria-hidden="true">▣</span></button> : <span className="slot-code-unavailable" aria-hidden="true">—</span>}</span>
          {['suspended', 'ended', 'expired'].includes(slot.state) ? <button type="button" className="primary-action" disabled={!canMutateSlot(slot) || Boolean(busy)} onClick={() => void mutateSlot(slot, 'play')}>{t('slots.play')}</button> : null}
          {['armed', 'active', 'stopping'].includes(slot.state) ? <button type="button" className="primary-action" disabled={!canMutateSlot(slot) || Boolean(busy)} onClick={() => void mutateSlot(slot, 'suspend')}>{slot.state === 'armed' ? t('slots.cancelArm') : t('slots.suspend')}</button> : null}
          <details className="slot-more-actions"><summary aria-label={t('slots.moreActions')}>⋯</summary><div className="slot-overflow-menu"><button type="button" className="secondary-action" disabled={!canMutateSlot(slot) || Boolean(busy)} onClick={() => slotAccessReady ? setConfirmAction({ kind: 'rotate', logicalAgentId: slot.logicalAgentId }) : void mutateAccess(slot, 'setup')}>{slotAccessReady ? t('slots.rotateAccessCode') : t('slots.setupAccessCode')}</button>
          <button type="button" className="destructive-action" disabled={!canMutateSlot(slot) || Boolean(busy) || ['deleting', 'deleted'].includes(slot.state)} onClick={() => setConfirmAction({ kind: 'delete', logicalAgentId: slot.logicalAgentId })}>{t('slots.delete')}</button>
          <Link className="nav-link" to={slotHref(slot)} onClick={rememberGlobalScroll}>{t('slots.details')}</Link></div></details>
        </div>
      </article>
    })}{slots.length === 0 && <FeedbackState variant="empty" title={t('slots.empty')} />}</div>}
  </section>
}
