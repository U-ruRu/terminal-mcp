import { meshPolicyMatches } from '../slots/meshPolicy'
import { MeshAccessPolicyControls, type MeshAccessPolicyRequest, type MeshAccessPolicyOutcome } from './MeshAccessPolicyControls'
import { connectionAuthorityScope } from '../connections/authorityScope'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, Navigate, useLocation, useParams } from 'react-router-dom'

import { ConsoleHttpError } from '../api/client'
import type { AccessMeshMutationRequest, AccessMeshSlotReadModel, ManagedFleetControlReadModel, ManagedFleetMutationResult, ManagedSlotStatusReadModel, PersistentAuditReadModel, PersistentMutationResult } from '../api/models'
import { clearAccessCode, loadAccessCode, saveAccessCode } from '../access/codeVault'
import { loadCachedFleetControlForProfile, propagateCachedFleetControl, saveCachedFleetControl } from '../connections/controlState'
import type { FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { meshPersistentSlotRoute, slotRoute } from '../navigation/routes'
import { Icon } from '../components/Icon'
import { ConfirmationDialog, FeedbackState, IconButton, IconButtonRow, UiButton } from '../components/UiPrimitives'
import { useAccessMeshState, slotReadFailure, type AccessMeshSlotsLoader, type AccessMeshDefaultsLoader, type AccessMeshMutator } from '../slots/accessMeshState'
import { acceptSlotOperation, beginSlotOperation, clearSlotOperation, loadSlotOperation, newSlotOperationKey, type PendingSlotOperation } from '../slots/operationJournal'
import { nativeCodeScope, readNativeCodeReceipt } from '../slots/nativeCodeReceipt'
import { contextsFromTopology, meshControlForContext, normalizedOrigin, resolveContextKey } from '../slots/topology'
import { AccessMeshDefaultsControls, AccessMeshSlotControls, type AccessMeshActionResult } from './AccessMeshPolicyControls'

// These callback types remain shared with other screens. This screen only reads current Access slots.
export type PersistentMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>
export type FleetControlLoader = (instanceId: string) => Promise<ManagedFleetControlReadModel>
export type FleetControlMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<ManagedFleetMutationResult>
export type ServerSlotsProps = {
  activeSessionsOnly?: boolean
  instances: FleetInstanceView[]
  loadFleetControl?: FleetControlLoader
  mutateFleetControl?: FleetControlMutator
  loadAccessMeshSlots?: AccessMeshSlotsLoader
  loadAccessMeshDefaults?: AccessMeshDefaultsLoader
  mutateAccessMesh?: AccessMeshMutator
  // Transitional call-site types; neither the old readers nor old mutators are called here.
  mutatePersistent?: PersistentMutator
  loadSlotAudit?: (instanceId: string, logicalAgentId: string) => Promise<PersistentAuditReadModel[]>
  loadManagedSlotStatus?: (instanceId: string, logicalAgentId: string) => Promise<ManagedSlotStatusReadModel>
}

type Entry = { slot: AccessMeshSlotReadModel; instanceId: string; contextKey: string; observedAt: number }
type SlotReference = { instanceId: string; issuerNodeId: string; slotId: string; logicalAgentId: string }
type Confirmation = SlotReference & { kind: 'rotate' | 'delete'; revision: number; publicName: string }
const stateKeys: Record<string, MessageKey> = {
  active: 'slots.state.active', idle: 'slots.state.idle', warning: 'slots.state.warning', draining: 'slots.state.draining',
  suspended: 'slots.state.suspended', cooldown: 'slots.state.cooldown', expired: 'slots.state.expired', deleted: 'slots.state.deleted', ended: 'slots.state.ended',
}
function codeFor(entry: Entry) {
  return loadAccessCode(entry.slot.logicalAgentId, entry.slot.codeGeneration ?? 0, window.localStorage, nativeCodeScope(entry.slot.issuerNodeId, entry.slot.slotId), entry.slot.revision)
}
function reference(entry: Entry): SlotReference { return { instanceId: entry.instanceId, issuerNodeId: entry.slot.issuerNodeId, slotId: entry.slot.slotId, logicalAgentId: entry.slot.logicalAgentId } }
function safeCode(value: unknown): string { return typeof value === 'string' && /^[a-z][a-z0-9_:-]{0,127}$/.test(value) ? value : 'request_failed' }
function sameSlot(entry: Entry, value: SlotReference): boolean { return entry.instanceId === value.instanceId && entry.slot.slotId === value.slotId && entry.slot.issuerNodeId === value.issuerNodeId }
async function writeClipboard(value: string): Promise<boolean> {
  try { if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(value); return true } } catch { /* Use the existing WebView fallback. */ }
  const element = document.createElement('textarea')
  element.value = value; element.style.position = 'fixed'; element.style.opacity = '0'
  document.body.appendChild(element); element.select()
  try { return typeof document.execCommand === 'function' && document.execCommand('copy') }
  catch { return false }
  finally { element.remove() }
}
function timer(entry: Entry, now: number, locale: ReturnType<typeof useI18n>['locale']) {
  const cycle = entry.slot.sessionLifecycle
  if (!cycle.hardExpiresAt || cycle.remainingSeconds === undefined || !['active', 'warning', 'draining'].includes(cycle.state)) return { text: '', cue: 'normal' as const }
  const remaining = Math.max(0, Math.floor(cycle.remainingSeconds - Math.max(0, now - entry.observedAt) / 1000))
  const h = Math.floor(remaining / 3600), m = Math.floor(remaining % 3600 / 60), s = remaining % 60
  const units = locale === 'ru' ? ['ч', 'м', 'с'] : locale === 'ka' ? ['სთ', 'წთ', 'წმ'] : ['h', 'min', 's']
  const text = h > 0 ? `${h}${units[0]} ${String(m).padStart(2, '0')}${units[1]} ${String(s).padStart(2, '0')}${units[2]}` : m > 0 ? `${m}${units[1]} ${String(s).padStart(2, '0')}${units[2]}` : `${s}${units[2]}`
  const policy = entry.slot.windowPolicy ?? entry.slot.policy
  // Existing color classes, not a separate native alert policy field.
  const cue = remaining <= policy.drainingSeconds ? 'alert' as const : remaining <= policy.warningSeconds ? 'warning' as const : 'normal' as const
  return { text, cue }
}

export function ServerSlots({ activeSessionsOnly = false, instances, loadFleetControl, mutateFleetControl, loadAccessMeshSlots, loadAccessMeshDefaults, mutateAccessMesh }: ServerSlotsProps) {
  const { t, locale } = useI18n()
  const location = useLocation()
  const { instanceId: routeInstanceId = '', meshId: routeMeshId = '', logicalAgentId } = useParams()
  const globalMode = !routeInstanceId && !routeMeshId
  const query = new URLSearchParams(location.search)
  const [filterKey, setFilterKey] = useState(query.get('filter') ?? sessionStorage.getItem('slots-list-filter') ?? 'all')
  const [settingsKey, setSettingsKey] = useState(query.get('settings') ?? sessionStorage.getItem('slots-settings-context') ?? '')
  const [createChoice, setCreateChoice] = useState('')
  const targets = JSON.stringify(instances.map((item) => ({ id: item.profile.instanceId, connected: item.runtime.authStatus === 'connected', authority: connectionAuthorityScope(item.profile) })).sort((a, b) => a.id.localeCompare(b.id)))
  const [controlCache, setControlCache] = useState<{
    targets: string
    controls: Record<string, ManagedFleetControlReadModel | undefined>
    errors: Record<string, string | undefined>
    confirmed: Record<string, boolean | undefined>
    observedAt: Record<string, number | undefined>
  }>(() => ({ targets, controls: Object.fromEntries(instances.map((item) => [item.profile.instanceId, loadCachedFleetControlForProfile(item.profile.instanceId, item.profile.origin)?.control])), errors: {}, confirmed: {}, observedAt: {} }))
  if (controlCache.targets !== targets) setControlCache({ targets, controls: {}, errors: {}, confirmed: {}, observedAt: {} })
  const controls = controlCache.targets === targets ? controlCache.controls : {}
  const controlErrors = controlCache.targets === targets ? controlCache.errors : {}
  const controlsConfirmed = controlCache.targets === targets ? controlCache.confirmed : {}
  const controlSerial = useRef<Record<string, number>>({})
  const controlInFlight = useRef<Record<string, number | undefined>>({})
  const mounted = useRef(true)
  const { states: reads, refresh: refreshNative } = useAccessMeshState(instances, loadAccessMeshSlots, loadAccessMeshDefaults)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const refreshControl = useCallback(async (instanceId: string) => {
    const currentTargets: Array<{ id: string; connected: boolean }> = JSON.parse(targets)
    if (!loadFleetControl || !currentTargets.some((item) => item.id === instanceId && item.connected)) return undefined
    const serial = (controlSerial.current[instanceId] ?? 0) + 1
    controlSerial.current[instanceId] = serial
    controlInFlight.current[instanceId] = serial
    setControlCache((current) => current.targets === targets ? { ...current, confirmed: { ...current.confirmed, [instanceId]: false } } : current)
    try {
      const value = await loadFleetControl(instanceId)
      if (!mounted.current || serial !== controlSerial.current[instanceId]) return undefined
      saveCachedFleetControl(instanceId, value, Date.now()); propagateCachedFleetControl(value, Date.now())
      setControlCache((current) => current.targets === targets ? { ...current, controls: { ...current.controls, [instanceId]: value }, errors: { ...current.errors, [instanceId]: undefined }, confirmed: { ...current.confirmed, [instanceId]: true }, observedAt: { ...current.observedAt, [instanceId]: Date.now() } } : current)
      return value
    } catch (error) {
      if (mounted.current && serial === controlSerial.current[instanceId]) setControlCache((current) => current.targets === targets ? { ...current, errors: { ...current.errors, [instanceId]: slotReadFailure(error).code }, confirmed: { ...current.confirmed, [instanceId]: false } } : current)
      return undefined
    } finally {
      if (controlInFlight.current[instanceId] === serial) delete controlInFlight.current[instanceId]
    }
  }, [loadFleetControl, targets])
  useEffect(() => {
    const values: Array<{ id: string; connected: boolean }> = JSON.parse(targets)
    for (const value of values) if (value.connected) void refreshControl(value.id)
    const poll = () => {
      if (document.visibilityState === 'hidden') return
      for (const value of values) if (value.connected && !controlInFlight.current[value.id]) void refreshControl(value.id)
    }
    const timer = window.setInterval(poll, 15_000)
    document.addEventListener('visibilitychange', poll)
    const serials = controlSerial.current
    return () => {
      window.clearInterval(timer); document.removeEventListener('visibilitychange', poll)
      for (const value of values) serials[value.id] = (serials[value.id] ?? 0) + 1
    }
  }, [refreshControl, targets])

  const contexts = contextsFromTopology(instances, controls, reads)
  const filter = activeSessionsOnly ? 'all' : resolveContextKey(filterKey, contexts)
  const routeContext = routeMeshId ? contexts.find((item) => item.meshId === routeMeshId) : routeInstanceId ? contexts.find((item) => item.memberInstanceIds.includes(routeInstanceId)) : undefined
  const activeContext = globalMode ? contexts.find((item) => item.key === resolveContextKey(settingsKey, contexts)) ?? contexts[0] : routeContext
  const relevantIds = new Set(globalMode ? (filter === 'all' ? contexts : contexts.filter((item) => item.key === filter)).flatMap((item) => item.memberInstanceIds) : routeInstanceId ? [routeInstanceId] : routeContext?.memberInstanceIds ?? [])
  const allEntries: Entry[] = []
  for (const instance of instances) {
    const read = reads[instance.profile.instanceId]
    const context = contexts.find((item) => item.memberInstanceIds.includes(instance.profile.instanceId))
    for (const slot of read?.slots ?? []) allEntries.push({ slot, instanceId: instance.profile.instanceId, contextKey: context?.key ?? `unresolved:${instance.profile.instanceId}`, observedAt: read?.observedAt ?? Date.now() })
  }
  allEntries.sort((a, b) => (Date.parse(b.slot.createdAt ?? '') || 0) - (Date.parse(a.slot.createdAt ?? '') || 0) || b.slot.revision - a.slot.revision)
  const distinct = new Map<string, Entry>()
  for (const entry of allEntries.filter((item) => relevantIds.has(item.instanceId))) {
    const key = entry.slot.issuerNodeId + ':' + entry.slot.slotId
    const current = distinct.get(key)
    if (!current || entry.slot.revision > current.slot.revision) distinct.set(key, entry)
  }
  const entries = [...distinct.values()]
  const owner = resolveContextKey(query.get('owner') ?? '', contexts)
  const selectedCandidates = logicalAgentId ? entries.filter((entry) => entry.slot.logicalAgentId === logicalAgentId && (!owner || entry.contextKey === owner) && (!query.get('issuer') || entry.slot.issuerNodeId === query.get('issuer')) && (!query.get('slot') || entry.slot.slotId === query.get('slot'))) : []
  const selected = selectedCandidates.length === 1 ? selectedCandidates[0] : undefined
  const selectedInstance = selected ? instances.find((item) => item.profile.instanceId === selected.instanceId) : undefined
  const policyInstanceId = routeInstanceId || activeContext?.instanceId || ''
  const policyInstance = instances.find((item) => item.profile.instanceId === policyInstanceId)
  const memberIds = routeInstanceId ? [routeInstanceId] : activeContext?.memberInstanceIds ?? []
  const createInstanceId = memberIds.includes(createChoice) ? createChoice : routeInstanceId || activeContext?.instanceId || ''
  const createInstance = instances.find((item) => item.profile.instanceId === createInstanceId)
  const createRead = reads[createInstanceId]
  const meshControl = meshControlForContext(activeContext, controls)
  const authorityNode = meshControl?.nodes.find((item) => item.nodeId === meshControl.controlNodeId)
  const meshAuthority = meshControl ? instances.find((item) => item.profile.instanceId === meshControl.controlNodeId || controls[item.profile.instanceId]?.nodeId === meshControl.controlNodeId || Boolean(authorityNode?.origin && normalizedOrigin(authorityNode.origin) === normalizedOrigin(item.profile.origin))) : undefined
  const defaults = reads[policyInstanceId]?.defaults

  const [busy, setBusy] = useState('')
  const lock = useRef(false)
  const [message, setMessage] = useState('')
  const [toastLeaving, setToastLeaving] = useState(false)
  const toastStartX = useRef<number | null>(null)
  const [, updateCodes] = useState(0)
  const [, updateOperations] = useState(0)
  const [created, setCreated] = useState<SlotReference | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [clock, setClock] = useState(() => Date.now())
  useEffect(() => { const id = window.setInterval(() => setClock(Date.now()), 1000); return () => window.clearInterval(id) }, [])
  useEffect(() => {
    if (!message) return
    const show = window.setTimeout(() => setToastLeaving(false), 0)
    const fade = window.setTimeout(() => setToastLeaving(true), 2600)
    const clear = window.setTimeout(() => setMessage(''), 3000)
    return () => { window.clearTimeout(show); window.clearTimeout(fade); window.clearTimeout(clear) }
  }, [message])
  useEffect(() => {
    const outside = (event: PointerEvent) => { document.querySelectorAll<HTMLDetailsElement>('.slot-more-actions[open]').forEach((item) => { if (event.target instanceof Node && !item.contains(event.target)) item.open = false }) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [])
  const rememberScroll = () => {
    if (!globalMode || activeSessionsOnly) return
    sessionStorage.setItem('slots-list-filter', filter)
    if (activeContext) sessionStorage.setItem('slots-settings-context', activeContext.key)
    sessionStorage.setItem(`slots-scroll:${filter}`, String(window.scrollY))
  }
  useEffect(() => {
    if (!globalMode || logicalAgentId) return
    const top = Number(sessionStorage.getItem(`slots-scroll:${filter}`) ?? '0')
    if (Number.isFinite(top) && top > 0) requestAnimationFrame(() => window.scrollTo({ top }))
  }, [filter, globalMode, logicalAgentId])

  const operationScope = (instanceId: string) => {
    const profile = instances.find((item) => item.profile.instanceId === instanceId)?.profile
    return profile ? connectionAuthorityScope(profile) : instanceId
  }
  const pendingFor = (instanceId: string): { value: PendingSlotOperation | null; unavailable: boolean } => {
    try { return { value: loadSlotOperation(operationScope(instanceId)), unavailable: false } } catch { return { value: null, unavailable: true } }
  }
  const blocked = (instanceId: string) => { const current = pendingFor(instanceId); return Boolean(current.value || current.unavailable) }
  const pending = instances.filter((item) => relevantIds.has(item.profile.instanceId)).map((item) => ({ instanceId: item.profile.instanceId, name: item.profile.displayName, ...pendingFor(item.profile.instanceId) })).filter((item) => item.value || item.unavailable)
  const finish = (instanceId: string) => { clearSlotOperation(operationScope(instanceId)); updateOperations((value) => value + 1) }
  const hasFreshWriteContext = (instanceId: string) => Boolean(mutateAccessMesh && instances.some((item) => item.profile.instanceId === instanceId && item.runtime.authStatus === 'connected' && item.profile.metadata.scope.split(/\s+/).includes('terminal:execute')) && reads[instanceId]?.slots && !reads[instanceId]?.slotsError && !reads[instanceId]?.slotsPending && clock - (reads[instanceId]?.observedAt ?? 0) <= 30_000)
  const canMutate = (instanceId: string) => hasFreshWriteContext(instanceId) && !blocked(instanceId)
  const execute = async (instanceId: string, request: AccessMeshMutationRequest, recover = false): Promise<AccessMeshActionResult> => {
    if (!mutateAccessMesh || lock.current || !hasFreshWriteContext(instanceId) || (!recover && blocked(instanceId)) || !instances.some((item) => item.profile.instanceId === instanceId && item.runtime.authStatus === 'connected')) return { result: { ok: false, code: 'operation_in_progress', payload: {} }, confirmed: false }
    if (request.action === 'policy' || request.action === 'defaults') {
      const target = request.action === 'defaults' ? reads[instanceId]?.defaults : reads[instanceId]?.slots?.find((slot) => slot.slotId === request.slot_id)
      if (!target?.inheritanceSupported || target.inheritance?.appliesTo !== 'next_window') return { result: { ok: false, code: 'unsupported_policy_semantics', payload: {} }, confirmed: false }
    }
    lock.current = true; setBusy(request.action === 'create' ? 'create' : `${instanceId}:${request.action}`); setMessage('')
    const scope = operationScope(instanceId)
    try {
      if (recover) {
        const current = loadSlotOperation(scope)
        if (current?.backend !== 'access-mesh' || JSON.stringify(current.request) !== JSON.stringify(request)) throw new Error('slot_operation_unresolved')
      } else beginSlotOperation(scope, { backend: 'access-mesh', request, startedAt: new Date().toISOString(), phase: 'pending' })
      updateOperations((value) => value + 1)
      const result = await mutateAccessMesh(instanceId, request)
      if (!result.ok) {
        if (result.payload.outcome === 'not_committed' || /revision_conflict|parent.*conflict|invalid_|_invalid|denied|forbidden|disabled|not_found|unsupported|auth_required/.test(result.code ?? '')) finish(instanceId)
        await refreshNative(instanceId); setMessage(safeCode(result.code ?? result.error))
        return { result, confirmed: false }
      }
      acceptSlotOperation(scope)
      let receipt: ReturnType<typeof readNativeCodeReceipt> | undefined
      if (request.action === 'create' || request.action === 'rotate') {
        receipt = readNativeCodeReceipt(result.payload)
        if ('slot_id' in request && receipt.slotId !== request.slot_id) throw new Error('invalid_code_receipt')
        const codeScope = nativeCodeScope(receipt.issuerNodeId, receipt.slotId)
        const prior = loadAccessCode(receipt.logicalAgentId, 0, window.localStorage, codeScope)
        if (!prior || prior.generation <= receipt.value.generation) saveAccessCode(receipt.logicalAgentId, receipt.value, window.localStorage, codeScope)
        updateCodes((value) => value + 1)
      }
      const fresh = await refreshNative(instanceId)
      const revision = typeof result.payload.revision === 'number' ? result.payload.revision : 0
      const freshSlot = fresh?.slots?.find((item) => item.slotId === (receipt?.slotId ?? ('slot_id' in request ? request.slot_id : '')))
      const confirmed = request.action === 'defaults'
        ? Boolean(fresh?.defaults && !fresh.defaultsError && fresh.defaults.revision >= revision && revision > 0)
        : Boolean(fresh?.slots && !fresh.slotsError && revision > 0 && (freshSlot && freshSlot.revision >= revision || request.action === 'delete' && !freshSlot))
      if (confirmed) {
        finish(instanceId)
        if (receipt && freshSlot) {
          const current = loadAccessCode(receipt.logicalAgentId, freshSlot.codeGeneration ?? 0, window.localStorage, nativeCodeScope(receipt.issuerNodeId, receipt.slotId), freshSlot.revision)
          if (current) {
            setCreated({ instanceId, logicalAgentId: receipt.logicalAgentId, issuerNodeId: receipt.issuerNodeId, slotId: receipt.slotId })
            if (request.action === 'create' && !recover) await writeClipboard(current.code)
          } else setCreated(null)
        }
        if (request.action === 'delete') {
          const old = allEntries.find((entry) => entry.instanceId === instanceId && entry.slot.slotId === request.slot_id)
          if (old) clearAccessCode(old.slot.logicalAgentId, window.localStorage, nativeCodeScope(old.slot.issuerNodeId, old.slot.slotId))
          updateCodes((value) => value + 1)
        }
        setMessage(t('slots.managed.confirmed'))
      } else setMessage(t('slots.managed.confirmationPending'))
      return { result, confirmed }
    } catch (error) {
      if (error instanceof ConsoleHttpError && [400, 401, 403, 404, 405, 409, 422].includes(error.status)) { finish(instanceId); await refreshNative(instanceId); setMessage(safeCode(error.code)) }
      else setMessage(t(error instanceof Error && error.message === 'operation_journal_unavailable' ? 'slots.operation.storage' : 'slots.managed.uncertain'))
      updateOperations((value) => value + 1)
      throw error
    } finally { lock.current = false; setBusy('') }
  }
  const copy = async (entry: Entry) => { const value = codeFor(entry); setMessage(value ? await writeClipboard(value.code) ? t('slots.accessCodeCopied') : t('slots.copyFailed') : t('slots.accessCodeUnavailable')) }
  const changeState = (entry: Entry, action: 'resume' | 'suspend') => void execute(entry.instanceId, { action, slot_id: entry.slot.slotId, expected_revision: entry.slot.revision, idempotency_key: newSlotOperationKey() }).catch(() => undefined)
  const showConfirmation = (entry: Entry, kind: Confirmation['kind']) => setConfirmation({ ...reference(entry), kind, revision: entry.slot.revision, publicName: entry.slot.publicName })
  const confirm = () => {
    const current = confirmation; setConfirmation(null)
    if (!current) return
    void execute(current.instanceId, { action: current.kind, slot_id: current.slotId, expected_revision: current.revision, idempotency_key: newSlotOperationKey() }).catch(() => undefined)
  }
  const href = (entry: Entry) => {
    const params = new URLSearchParams({ issuer: entry.slot.issuerNodeId, slot: entry.slot.slotId })
    if (activeSessionsOnly) {
      const context = contexts.find((item) => item.key === entry.contextKey)
      return `${context?.meshId ? meshPersistentSlotRoute(context.meshId, entry.slot.logicalAgentId) : slotRoute(entry.instanceId, entry.slot.logicalAgentId)}?${params}`
    }
    if (globalMode) {
      if (filter !== 'all') params.set('filter', filter)
      if (activeContext) params.set('settings', activeContext.key)
      params.set('owner', entry.contextKey)
      return `/slots/${encodeURIComponent(entry.slot.logicalAgentId)}?${params}`
    }
    const path = routeMeshId ? meshPersistentSlotRoute(routeMeshId, entry.slot.logicalAgentId) : slotRoute(entry.instanceId, entry.slot.logicalAgentId)
    return `${path}?${params}`
  }
  const positionMenu = (details: HTMLDetailsElement) => {
    if (!details.open) { details.classList.remove('slot-more-actions-up'); return }
    document.querySelectorAll<HTMLDetailsElement>('.slot-more-actions[open]').forEach((other) => { if (other !== details) other.open = false })
    requestAnimationFrame(() => {
      const trigger = details.querySelector<HTMLElement>('summary'), menu = details.querySelector<HTMLElement>('.slot-overflow-menu')
      if (!details.open || !trigger || !menu) return
      const rect = trigger.getBoundingClientRect(), navTop = document.querySelector<HTMLElement>('.mobile-bottom-navigation')?.getBoundingClientRect().top
      const bottom = window.visualViewport ? window.visualViewport.offsetTop + window.visualViewport.height : window.innerHeight
      const safeBottom = navTop && navTop > 0 ? Math.min(navTop, bottom) : bottom
      details.classList.toggle('slot-more-actions-up', safeBottom - rect.bottom < menu.getBoundingClientRect().height + 8 && rect.top > safeBottom - rect.bottom)
    })
  }
  const refreshVisible = () => { for (const id of relevantIds) { void refreshNative(id); void refreshControl(id) } }
  const createdEntry = created ? allEntries.find((entry) => sameSlot(entry, created)) : undefined
  const createdCode = createdEntry ? codeFor(createdEntry) : null
  const selectedCode = selected ? codeFor(selected) : null
  const createReady = Boolean(createRead?.slots && !createRead.slotsError && createRead.defaults && !createRead.defaultsError && !createRead.defaultsPending && canMutate(createInstanceId))
  const meshSettings = !routeInstanceId && activeContext?.kind === 'mesh'
  const nativeStandaloneProof = Boolean(defaults?.inheritanceSupported && defaults.meshPolicySupported === false
    && defaults.writeTarget === 'access_defaults' && defaults.inheritance?.source === 'standalone')
  const standaloneConfirmed = Boolean((controlsConfirmed[policyInstanceId] || nativeStandaloneProof)
    && (activeContext?.kind === 'server' || routeInstanceId && activeContext?.kind === 'mesh') && defaults?.writeTarget !== 'fleet_control')
  const defaultsWritable = Boolean(standaloneConfirmed && !reads[policyInstanceId]?.defaultsError && !reads[policyInstanceId]?.defaultsPending && canMutate(policyInstanceId))
  const allRead = relevantIds.size > 0 && [...relevantIds].every((id) => Boolean(reads[id]?.slots && !reads[id]?.slotsError))

  const authorityId = meshAuthority?.profile.instanceId ?? ''
  const authorityControl = controls[authorityId]
  const authorityRead = reads[authorityId]
  const meshPolicy = authorityControl?.policy ?? meshControl?.policy
  const meshSupported = Boolean(authorityRead?.defaults?.meshPolicySupported)
  const meshWritable = Boolean(meshSupported && mutateFleetControl && meshAuthority?.runtime.authStatus === 'connected'
    && meshAuthority.profile.metadata.scope.split(/\s+/).includes('terminal:execute') && controlsConfirmed[authorityId] && clock - (controlCache.observedAt[authorityId] ?? 0) <= 30_000
    && authorityControl?.managed && authorityControl.nodeId === authorityControl.controlNodeId
    && authorityRead?.defaults && !authorityRead.defaultsPending && !authorityRead.defaultsError
    && clock - (authorityRead.defaultsObservedAt ?? 0) <= 30_000)
  const saveMesh = async (request: MeshAccessPolicyRequest): Promise<MeshAccessPolicyOutcome> => {
    if (!meshWritable || !mutateFleetControl || lock.current) return { confirmed: false }
    lock.current = true; setBusy('mesh:policy')
    try {
      const result = await mutateFleetControl(authorityId, '/actions/fleet/control/policy', request)
      const fresh = await refreshControl(authorityId)
      if (!result.ok) return { confirmed: false, conflict: /conflict/.test(result.code ?? result.error ?? '') }
      const confirmed = meshPolicyMatches(fresh?.policy, request)
      // Each member rereads its own applied defaults; an authority receipt is not replica convergence.
      for (const id of activeContext?.memberInstanceIds ?? []) void refreshNative(id)
      return { confirmed, conflict: Boolean(fresh?.policy && fresh.policy.revision > request.expected_revision && !confirmed) }
    } catch (error) {
      const fresh = await refreshControl(authorityId)
      if (error instanceof ConsoleHttpError && error.status === 409) return { confirmed: false, conflict: true }
      // A lost receipt can be confirmed by a newer exact server read without resubmitting the mutation.
      if (meshPolicyMatches(fresh?.policy, request)) {
        for (const id of activeContext?.memberInstanceIds ?? []) void refreshNative(id)
        return { confirmed: true }
      }
      throw error
    } finally { lock.current = false; setBusy('') }
  }
  const operationFeedback = <>
    {!loadAccessMeshSlots || !loadAccessMeshDefaults ? <FeedbackState variant="partial" title={t('slots.native.unsupported')} /> : <div className="slot-context-statuses">{instances.filter((item) => relevantIds.has(item.profile.instanceId)).map((item) => {
      const read = reads[item.profile.instanceId]
      if (!read || (!read.slots && read.slotsPending)) return <p role="status" className="muted" key={item.profile.instanceId}>{item.profile.displayName} · {item.runtime.authStatus === 'connected' ? t('slots.native.loading') : t('slots.liveRequired')}</p>
      const failures = [read.slotsError, read.defaultsError].filter((error, index, values) => error && values.findIndex((value) => value?.kind === error.kind && value?.code === error.code) === index)
      return failures.map((error) => error ? <div className="attention-strip" role="status" key={item.profile.instanceId + ':' + error.kind + ':' + error.code}><strong>{item.profile.displayName}</strong><span>{t(error.kind === 'unsupported' ? 'slots.native.unsupported' : error.kind === 'disabled' ? 'slots.disabled' : error.kind === 'denied' ? 'slots.native.denied' : 'slots.native.failed')} · {error.code}</span><IconButton icon="retry" variant="quiet" label={t('slots.managed.refresh') + ' — ' + item.profile.displayName} disabled={Boolean(busy)} onClick={() => void refreshNative(item.profile.instanceId)} /></div> : null)
    })}</div>}
    {pending.map((item) => <div className="attention-strip slot-operation-recovery" role="status" key={item.instanceId}><strong>{item.name}</strong><p>{t(item.unavailable ? 'slots.operation.storage' : 'slots.operation.pending')}</p>{item.value?.backend === 'access-mesh' ? <IconButton icon="retry" variant="secondary" label={t('slots.operation.recover')} disabled={Boolean(busy) || !hasFreshWriteContext(item.instanceId)} onClick={() => { const operation = item.value; if (operation?.backend === 'access-mesh') void execute(item.instanceId, operation.request, true).catch(() => undefined) }} /> : null}</div>)}
    {message ? <div className={'transient-toast' + (toastLeaving ? ' transient-toast-exit' : '')} role="status" aria-live="polite" onPointerDown={(event) => { toastStartX.current = event.clientX }} onPointerUp={(event) => { if (toastStartX.current !== null && Math.abs(event.clientX - toastStartX.current) >= 48) setMessage(''); toastStartX.current = null }} onPointerCancel={() => { toastStartX.current = null }}><span>{message}</span></div> : null}
    {confirmation ? <ConfirmationDialog title={t(confirmation.kind === 'rotate' ? 'slots.rotateAccessCode' : 'slots.delete')} subject={confirmation.publicName} detail={t(confirmation.kind === 'rotate' ? 'slots.rotateAccessConfirm' : 'slots.deleteConfirm')} cancelLabel={t('slots.confirmCancel')} confirmLabel={t(confirmation.kind === 'rotate' ? 'slots.rotateAccessCode' : 'slots.delete')} confirmIcon={confirmation.kind === 'rotate' ? 'rotate' : 'delete'} confirmVariant={confirmation.kind === 'rotate' ? 'primary' : 'destructive'} onCancel={() => setConfirmation(null)} onConfirm={confirm} /> : null}
  </>
  if (activeSessionsOnly) {
    const active = entries.filter((entry) => {
      if (!['active', 'warning', 'draining', 'stopping'].includes(entry.slot.sessionLifecycle.state)) return false
      const remaining = entry.slot.sessionLifecycle.remainingSeconds
      return remaining !== undefined ? remaining > 0 && remaining - Math.max(0, clock - entry.observedAt) / 1000 > 0
        : Date.parse(entry.slot.sessionLifecycle.hardExpiresAt ?? '') > clock
    })
    const groups = contexts.filter((context) => active.some((entry) => entry.contextKey === context.key))
      .sort((a, b) => Number(a.kind !== 'mesh') - Number(b.kind !== 'mesh') || a.label.localeCompare(b.label))
    return <>
      {operationFeedback}
      {!instances.length || active.length === 0 && allRead ? <FeedbackState variant="empty" title={t('fleet.noActiveSharedSessions')} /> : null}
      {groups.map((group) => <section className="fleet-session-group" key={group.key} aria-label={group.label}>
        <h3 className="fleet-group-title">{group.label}</h3>
        <div className="slot-grid fleet-slot-grid">{active.filter((entry) => entry.contextKey === group.key).sort((a, b) => a.slot.publicName.localeCompare(b.slot.publicName)).map((entry) => {
          const saved = codeFor(entry), timing = timer(entry, clock, locale), writable = canMutate(entry.instanceId) && !busy
          return <article className={'panel slot-card slot-cue-' + timing.cue} key={entry.slot.issuerNodeId + ':' + entry.slot.slotId}>
            <div className="slot-card-primary">
              <Link className="text-link slot-card-identity" to={href(entry)}><strong>{entry.slot.publicName}</strong></Link>
              <span className="slot-timer">{timing.text}</span>
              <span className={'status server-status slot-session-badge slot-session-' + entry.slot.sessionLifecycle.state}>{t(stateKeys[entry.slot.sessionLifecycle.state] ?? 'slots.state.unknown')}</span>
            </div>
            <div className="slot-card-secondary">
              <span className="slot-code-cell">{saved ? <span className="access-code-copy"><code>{saved.code}</code><IconButton icon="copy" variant="quiet" label={t('slots.copyAccessCode') + ' — ' + entry.slot.publicName} onClick={() => void copy(entry)} /></span> : <span className="slot-code-unavailable">—</span>}</span>
              <IconButton icon="pause" variant="primary" label={t('slots.suspend')} disabled={!writable} onClick={() => changeState(entry, 'suspend')} />
              <details className="slot-more-actions" onToggle={(event) => positionMenu(event.currentTarget)}><summary aria-label={t('slots.moreActions')}><Icon name="more" /></summary><div className="slot-overflow-menu">
                <UiButton type="button" variant="secondary" disabled={!writable} onClick={() => showConfirmation(entry, 'rotate')}><Icon name="rotate" />{t('slots.rotateAccessCode')}</UiButton>
                <UiButton type="button" variant="destructive" disabled={!writable} onClick={() => showConfirmation(entry, 'delete')}><Icon name="delete" />{t('slots.delete')}</UiButton>
                <Link className="nav-link" to={href(entry)}>{t('slots.details')}</Link>
              </div></details>
            </div>
          </article>
        })}</div>
      </section>)}
    </>
  }
  if (routeInstanceId && !instances.some((item) => item.profile.instanceId === routeInstanceId)) return <Navigate to="/" replace />
  if (!instances.length) return <FeedbackState variant="empty" title={t('slots.empty')} />
  return <section className="stack compact-operational-surface slots-surface" aria-label={t('nav.slots')}>
    {globalMode && !logicalAgentId ? <div className="slots-global-filter surface-section"><label className="ui-field"><span>{t('slots.filter')}</span><select value={filter} onChange={(event) => setFilterKey(event.target.value)}><option value="all">{t('slots.all')}</option>{contexts.map((item) => <option key={item.key} value={item.key}>{item.label}{item.kind === 'unknown' ? ` · ${t('slots.policyScopeUnknown')}` : ''}</option>)}</select></label><IconButton icon="retry" variant="quiet" label={t('slots.native.refreshList')} disabled={Boolean(busy)} onClick={refreshVisible} /></div> : null}
    {operationFeedback}
    {!logicalAgentId && activeContext ? <article className="surface-section policy-controls">
      {globalMode ? <label className="ui-field slots-context-select"><span>{t('slots.settingsContext')}</span><select value={activeContext.key} onChange={(event) => setSettingsKey(event.target.value)}>{contexts.map((item) => <option key={item.key} value={item.key}>{item.label}{item.kind === 'unknown' ? ` · ${t('slots.policyScopeUnknown')}` : ''}</option>)}</select></label> : null}
      {meshSettings ? meshPolicy && meshSupported ? <MeshAccessPolicyControls key={authorityId + ':' + targets} policy={meshPolicy} authorityName={meshAuthority?.profile.displayName ?? meshControl?.controlNodeId ?? ''} canMutate={meshWritable} externalBusy={Boolean(busy)} save={saveMesh} refresh={() => { if (authorityId) { void refreshControl(authorityId); void refreshNative(authorityId) } }} /> : <FeedbackState variant="partial" title={t('slots.native.meshPolicyUnsupported')} /> : defaults ? <AccessMeshDefaultsControls key={policyInstanceId + ':' + targets} defaults={defaults} canMutate={defaultsWritable} externalBusy={Boolean(busy)} execute={(request) => execute(policyInstanceId, request)} refresh={() => void refreshNative(policyInstanceId)} /> : null}
      {activeContext.kind === 'unknown' ? <p className="attention-strip" role="status">{t('slots.policyScopeUnknown')}{controlErrors[policyInstanceId] ? ` · ${controlErrors[policyInstanceId]}` : ''}</p> : null}
      {policyInstance && policyInstance.runtime.status !== 'live' ? <p className="muted">{t('slots.cachedReadOnly')}</p> : null}
    </article> : null}
    {!logicalAgentId && activeContext && createRead?.defaults && !createRead.defaultsError ? <form className="slot-create surface-section" onSubmit={(event) => { event.preventDefault(); if (createReady) void execute(createInstanceId, { action: 'create', mode: 'persistent', idempotency_key: newSlotOperationKey() }).catch(() => undefined) }}><h3>{t('slots.createSection')}</h3><div className="slot-create-row">
      {memberIds.length > 1 ? <label className="ui-field"><span>{t('slots.native.createServer')}</span><select value={createInstanceId} onChange={(event) => setCreateChoice(event.target.value)} disabled={Boolean(busy)}>{memberIds.map((id) => <option key={id} value={id}>{instances.find((item) => item.profile.instanceId === id)?.profile.displayName ?? id}</option>)}</select></label> : <p className="muted">{createInstance?.profile.displayName}</p>}
      <p className="muted">{t('slots.native.assignedName')}</p><IconButton type="submit" icon="create" variant="primary" label={t('slots.create')} busy={busy === 'create'} disabled={!createReady || Boolean(busy)} />
    </div>{createdEntry && createdCode && createdEntry.instanceId === createInstanceId ? <div className="slot-access-code created-access-row" role="status"><span>{createdEntry.slot.publicName}</span><span className="access-code-copy"><code>{createdCode.code}</code><IconButton icon="copy" variant="quiet" label={t('slots.copyAccessCode') + ' — ' + createdEntry.slot.publicName} onClick={() => void copy(createdEntry)} /></span><p className="muted">{createdCode.expiresAt ? t('slots.native.codeExpiry') + ' ' + createdCode.expiresAt : t('slots.native.codeExpiryUnknown')}</p></div> : null}</form> : null}
    {logicalAgentId && !selected ? <FeedbackState variant="partial" title={t('slots.detailsUnavailable')} detail={t('slots.detailsUnavailableHint')} /> : null}
    {selected ? <article className="panel slot-detail" aria-label={t('title.slotDetail')}><div className="section-heading"><div><p className="eyebrow">{t('slots.accessPublicName')}</p><h3>{selected.slot.publicName}</h3></div><span className={'status server-status slot-session-badge slot-session-' + selected.slot.sessionLifecycle.state}>{t(stateKeys[selected.slot.sessionLifecycle.state] ?? 'slots.state.unknown')}</span></div>
      <div className="slot-access-code"><span>{t('slots.accessCodeTitle')}</span>{selectedCode ? <><code>{selectedCode.code}</code><IconButton icon="copy" variant="quiet" label={t('slots.copyAccessCode') + ' — ' + selected.slot.publicName} onClick={() => void copy(selected)} /></> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</div>
      {selectedCode ? <p className="muted">{selectedCode.expiresAt ? t('slots.native.codeExpiry') + ' ' + selectedCode.expiresAt : t('slots.native.codeExpiryUnknown')}{selected.slot.codeGeneration === undefined ? ` ${t('slots.native.codeValidationLimited')}` : ''}</p> : null}
      <details className="technical-details"><summary>{t('slots.technicalDetails')}</summary><dl className="slot-details-grid"><div><dt>{t('slots.native.slotId')}</dt><dd><code>{selected.slot.slotId}</code></dd></div><div><dt>{t('slots.logicalAgentId')}</dt><dd><code>{selected.slot.logicalAgentId}</code></dd></div><div><dt>{t('slots.authority')}</dt><dd>{selected.slot.issuerNodeId}</dd></div><div><dt>{t('slots.rawState')}</dt><dd><code>{selected.slot.state}</code></dd></div><div><dt>{t('slots.revision')}</dt><dd>{selected.slot.revision}</dd></div><div><dt>{t('slots.accessGeneration')}</dt><dd>{selected.slot.codeGeneration ?? t('common.unavailable')}</dd></div><div><dt>{t('slots.createdAt')}</dt><dd>{selected.slot.createdAt ?? t('common.unavailable')}</dd></div><div><dt>{t('slots.updatedAt')}</dt><dd>{selected.slot.updatedAt ?? t('common.unavailable')}</dd></div></dl></details>
      <IconButtonRow className="server-actions"><IconButton icon="rotate" variant="secondary" label={t('slots.rotateAccessCode')} disabled={!canMutate(selected.instanceId) || Boolean(busy) || selected.slot.state === 'deleted'} onClick={() => showConfirmation(selected, 'rotate')} /></IconButtonRow>
      {selectedInstance?.runtime.authStatus !== 'connected' ? <p className="attention-strip">{t('slots.liveRequired')}</p> : null}
      <AccessMeshSlotControls key={selected.instanceId + ':' + selected.slot.slotId + ':' + targets} slot={selected.slot} canMutate={canMutate(selected.instanceId) && selected.slot.state !== 'deleted'} externalBusy={Boolean(busy)} execute={(request) => execute(selected.instanceId, request)} refresh={() => void refreshNative(selected.instanceId)} />
    </article> : null}
    {!logicalAgentId ? <div className="slot-grid">{entries.map((entry) => {
      const value = codeFor(entry), timing = timer(entry, clock, locale)
      const state = entry.slot.sessionLifecycle.state, writable = canMutate(entry.instanceId) && entry.slot.state !== 'deleted' && !busy
      return <article className={'panel slot-card slot-cue-' + timing.cue} key={entry.slot.issuerNodeId + ':' + entry.slot.slotId}><div className="slot-card-row"><div className="slot-identity"><Link className="slot-name-link" to={href(entry)} onClick={rememberScroll}><strong>{entry.slot.publicName}</strong></Link><span className={'status server-status slot-session-badge slot-session-' + state}>{t(stateKeys[state] ?? 'slots.state.unknown')}</span></div>
        <span className="slot-code-cell">{value ? <span className="access-code-copy"><code>{value.code}</code><IconButton icon="copy" variant="quiet" label={t('slots.copyAccessCode') + ' — ' + entry.slot.publicName} onClick={() => void copy(entry)} /></span> : <span className="slot-code-unavailable" aria-hidden="true">—</span>}</span>
        <div className="slot-card-actions"><span className={'slot-timer slot-timer-' + timing.cue} role={timing.cue === 'normal' ? undefined : 'status'}>{timing.cue === 'warning' ? <Icon name="warning" /> : timing.cue === 'alert' ? <Icon name="error" /> : null}{timing.text || '\u00a0'}</span>
          {entry.slot.state === 'suspended' ? <IconButton icon="play" variant="primary" label={t('slots.play')} disabled={!writable} onClick={() => changeState(entry, 'resume')} /> : entry.slot.state === 'active' ? <IconButton icon="pause" variant="primary" label={t('slots.suspend')} disabled={!writable} onClick={() => changeState(entry, 'suspend')} /> : null}
          <details className="slot-more-actions" onToggle={(event) => positionMenu(event.currentTarget)}><summary aria-label={t('slots.moreActions')}><Icon name="more" /></summary><div className="slot-overflow-menu" onClick={(event) => { if ((event.target as HTMLElement).closest('button,a')) (event.currentTarget.closest('details') as HTMLDetailsElement | null)?.removeAttribute('open') }}><UiButton type="button" variant="secondary" disabled={!writable} onClick={() => showConfirmation(entry, 'rotate')}><Icon name="rotate" />{t('slots.rotateAccessCode')}</UiButton><UiButton type="button" variant="destructive" disabled={!writable} onClick={() => showConfirmation(entry, 'delete')}><Icon name="delete" />{t('slots.delete')}</UiButton><Link className="nav-link" to={href(entry)} onClick={rememberScroll}>{t('slots.details')}</Link></div></details>
        </div></div></article>
    })}{entries.length === 0 && allRead ? <FeedbackState variant="empty" title={t('slots.empty')} /> : null}</div> : null}
  </section>
}
