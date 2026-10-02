import { useEffect, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import type { ManagedFleetControlReadModel, ManagedFleetMutationResult, PersistentAuditReadModel, PersistentMutationResult, PersistentSlotReadModel } from '../api/models'
import { clearAccessCode, loadAccessCode, saveAccessCode } from '../access/codeVault'
import { loadCachedFleetControlForProfile, propagateCachedFleetControl, saveCachedFleetControl, type FleetControlFreshness } from '../connections/controlState'
import type { FleetInstanceView } from '../fleet/types'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { meshPersistentRoute, meshPersistentSlotRoute, meshRoute, serverRoute, slotRoute, slotsRoute, taskRoute } from '../navigation/routes'
import { FeedbackState } from '../components/UiPrimitives'

export type PersistentMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>
export type FleetControlLoader = (instanceId: string) => Promise<ManagedFleetControlReadModel>
export type FleetControlMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<ManagedFleetMutationResult>
function idempotencyKey(): string { return globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `console-${Date.now()}-${Math.random().toString(36).slice(2)}` }
function duration(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const rest = safe % 60
  const parts: string[] = []
  if (hours) parts.push(String(hours) + ' hr')
  if (minutes) parts.push(String(minutes) + ' min')
  if (rest || parts.length === 0) parts.push(String(rest) + ' sec')
  return parts.join(' ')
}
function parseDuration(value: string): number | null {
  const normalized = value.trim().toLowerCase()
  if (!normalized) return null
  if (/^\d+$/.test(normalized)) return Number(normalized) * 60
  const token = /(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)/g
  let seconds = 0; let matched = ''; let part: RegExpExecArray | null
  while ((part = token.exec(normalized)) !== null) {
    matched += part[0]
    const amount = Number(part[1]); const unit = part[2]
    seconds += unit.startsWith('h') ? amount * 3600 : unit.startsWith('m') ? amount * 60 : amount
  }
  return seconds > 0 && matched.replace(/\s+/g, '') === normalized.replace(/\s+/g, '') ? seconds : null
}
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
function normalizedOrigin(value: string | undefined): string { return (value ?? '').replace(/\/+$/, '').toLowerCase() }
function slotTiming(slot: PersistentSlotReadModel, authorityNowMs: number, durationSeconds: number, warningAfter: number, alertAfter: number) { if (!slot.workSession) return { text: '', cue: 'normal' as const }; const remaining = Math.max(0, Math.floor((Date.parse(slot.workSession.hardExpiresAt) - authorityNowMs) / 1000)); if (durationSeconds <= 0) return { text: duration(remaining), cue: 'normal' as const }; const elapsed = Math.max(0, durationSeconds - remaining); const cue = elapsed >= alertAfter ? 'alert' as const : elapsed >= warningAfter ? 'warning' as const : 'normal' as const; return { text: duration(remaining), cue } }

export function ServerSlots({ instances, mutatePersistent, loadSlotAudit, loadFleetControl, mutateFleetControl }: { instances: FleetInstanceView[]; mutatePersistent?: PersistentMutator; loadSlotAudit?: (instanceId: string, logicalAgentId: string) => Promise<PersistentAuditReadModel[]>; loadFleetControl?: FleetControlLoader; mutateFleetControl?: FleetControlMutator }) {
  const { t } = useI18n(); const { instanceId: routeInstanceId = '', logicalAgentId, meshId = '' } = useParams(); const navigate = useNavigate()
  const meshCandidates = meshId
    ? instances.map((candidate) => ({
        candidate,
        control: loadCachedFleetControlForProfile(candidate.profile.instanceId, candidate.profile.origin)?.control,
      })).filter(({ candidate, control }) => {
        if (!control?.managed) return false
        const localNode = control.nodes.find((node) => (
          node.nodeId === control.nodeId
          || normalizedOrigin(node.origin) === normalizedOrigin(candidate.profile.origin)
        ))
        return control.mesh?.meshId === meshId || localNode?.meshId === meshId
      }).sort((a, b) => Number(b.candidate.runtime.authStatus === 'connected') - Number(a.candidate.runtime.authStatus === 'connected'))
    : []
  const instance = meshId ? meshCandidates[0]?.candidate : instances.find((item) => item.profile.instanceId === routeInstanceId)
  const instanceId = instance?.profile.instanceId ?? routeInstanceId
  const persistent = instance?.runtime.realtime?.snapshot?.persistent; const slots = persistent?.slots ?? []; const selected = logicalAgentId ? slots.find((slot) => slot.logicalAgentId === logicalAgentId) : undefined
  const [clock, setClock] = useState(() => Date.now()); const [anchor, setAnchor] = useState(() => ({ server: '', serverMs: Date.now(), localMs: Date.now() })); const [busy, setBusy] = useState(''); const [message, setMessage] = useState(''); const [createName, setCreateName] = useState(''); const [reassignTarget, setReassignTarget] = useState('')
  const [accessCodeRevision, setAccessCodeRevision] = useState(0)
  const [createdAccess, setCreatedAccess] = useState<{ logicalAgentId: string; displayName: string; code: string; generation: number } | null>(null)
  const [policyDraft, setPolicyDraft] = useState<{ key: string; duration: string; warning: string; alert: string; rearm: string } | null>(null)
  const [loadedAudit, setLoadedAudit] = useState<{ key: string; values?: PersistentAuditReadModel[]; error?: string }>({ key: '' })
  const [fleetControl, setFleetControl] = useState<ManagedFleetControlReadModel | null>(() => loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)?.control ?? null)
  const [fleetControlFreshness, setFleetControlFreshness] = useState<FleetControlFreshness>(() => loadCachedFleetControlForProfile(instanceId, instance?.profile.origin) ? 'stale' : 'unknown')
  const [legacyOverride, setLegacyOverride] = useState<{ value: boolean; phase: 'pending' | 'confirmed' | 'failed' } | null>(null)
  useEffect(() => {
    let cancelled = false
    const handle = window.setTimeout(() => {
      const cached = loadCachedFleetControlForProfile(instanceId, instance?.profile.origin)
      setFleetControl(cached?.control ?? null)
      setFleetControlFreshness(cached ? 'stale' : 'unknown')
      setLegacyOverride(null)
      if (!loadFleetControl || instance?.runtime.authStatus !== 'connected') return
      void loadFleetControl(instanceId)
        .then((control) => {
          if (cancelled) return
          const observedAt = Date.now()
          saveCachedFleetControl(instanceId, control, observedAt)
          propagateCachedFleetControl(control, observedAt)
          setFleetControl(control)
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
  }, [instance?.profile.origin, instance?.runtime.authStatus, instanceId, loadFleetControl])

  const selectedLogicalAgentId = selected?.logicalAgentId
  useEffect(() => {
    if (!selectedLogicalAgentId || !loadSlotAudit) return
    const key = instanceId + ':' + selectedLogicalAgentId
    let cancelled = false
    void loadSlotAudit(instanceId, selectedLogicalAgentId)
      .then((values) => { if (!cancelled) setLoadedAudit({ key, values }) })
      .catch((error: unknown) => {
        if (!cancelled) setLoadedAudit({ key, error: error instanceof Error ? error.message : 'slot_audit_unavailable' })
      })
    return () => { cancelled = true }
  }, [instanceId, loadSlotAudit, selectedLogicalAgentId])
  const selectedAuditKey = selected ? instanceId + ':' + selected.logicalAgentId : ''
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
  const effectiveLegacyAdmission = legacyOverride && legacyOverride.phase !== 'failed'
    ? legacyOverride.value
    : effectivePolicy?.legacyAdmissionEnabled
  const managedMeshName = (meshId ? fleetControl?.meshes.find((mesh) => mesh.meshId === meshId)?.displayName : undefined)
    ?? fleetControl?.mesh?.displayName
    ?? (fleetControl?.managed
      ? fleetControl.nodes.find((node) => node.nodeId === fleetControl.nodeId)?.meshId
        ? fleetControl.meshes.find((mesh) => mesh.meshId === fleetControl.nodes.find((node) => node.nodeId === fleetControl.nodeId)?.meshId)?.displayName
        : undefined
      : undefined)
  const policyKey = effectivePolicy ? [effectivePolicy.durationSeconds, effectivePolicy.warningAfterSeconds, effectivePolicy.alertAfterSeconds, effectivePolicy.rearmAfterSeconds].join(':') : ''
  const draft = policyDraft?.key === policyKey ? policyDraft : { key: policyKey, duration: effectivePolicy ? duration(effectivePolicy.durationSeconds) : '', warning: effectivePolicy ? duration(effectivePolicy.warningAfterSeconds) : '', alert: effectivePolicy ? duration(effectivePolicy.alertAfterSeconds) : '', rearm: effectivePolicy ? duration(effectivePolicy.rearmAfterSeconds) : '' }
  // Read freshness and write reachability are separate planes. A cached projection must not
  // suppress an authenticated direct-authority mutation attempt.
  const canMutate = Boolean(mutatePersistent && persistent?.enabled && instance?.runtime.authStatus === 'connected')
  const policyCanMutate = managedPolicy
    ? Boolean(mutateFleetControl && managedAuthorityInstanceId && managedAuthorityInstance?.runtime.authStatus === 'connected')
    : standaloneConfirmed ? canMutate : false
  const mutation = async (key: string, path: string, body: Record<string, unknown>) => { if (!canMutate || !mutatePersistent) { setMessage(t('slots.liveRequired')); return false } setBusy(key); setMessage(''); try { const result = await mutatePersistent(instanceId, path, body); if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return false } setMessage(t('slots.mutationApplied')); return true } catch (error) { setMessage(error instanceof Error ? error.message : t('slots.mutationFailed')); return false } finally { setBusy('') } }
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
  const mutateSlot = (slot: PersistentSlotReadModel, action: 'play' | 'suspend' | 'delete') => mutation(`${slot.logicalAgentId}:${action}`, `/actions/persistent/slots/${action}`, { logical_agent_id: slot.logicalAgentId, expected_revision: slot.slotRevision, idempotency_key: idempotencyKey() })
  const timingPolicyLocked = slots.some((slot) => ['armed', 'active', 'stopping'].includes(slot.state))
  const saveTimingPolicy = async () => {
    const durationSeconds = parseDuration(draft.duration); const warningAfterSeconds = parseDuration(draft.warning); const alertAfterSeconds = parseDuration(draft.alert); const rearmAfterSeconds = parseDuration(draft.rearm)
    if (durationSeconds === null || warningAfterSeconds === null || alertAfterSeconds === null || rearmAfterSeconds === null || !(0 < warningAfterSeconds && warningAfterSeconds < alertAfterSeconds && alertAfterSeconds < durationSeconds) || rearmAfterSeconds <= 0) {
      setMessage(t('slots.policyInvalid')); return
    }
    if (managedPolicy) {
      await managedPolicyMutation('policy', '/actions/fleet/control/policy', {
        duration_seconds: durationSeconds,
        warning_after_seconds: warningAfterSeconds,
        alert_after_seconds: alertAfterSeconds,
        rearm_after_seconds: rearmAfterSeconds,
        legacy_admission_enabled: managedPolicy.legacyAdmissionEnabled,
        expected_revision: managedPolicy.revision,
      })
      return
    }
    await mutation('policy', '/actions/persistent/policy', { duration_seconds: durationSeconds, warning_after_seconds: warningAfterSeconds, alert_after_seconds: alertAfterSeconds, rearm_after_seconds: rearmAfterSeconds })
  }
  const toggleLegacy = async () => {
    if (!effectivePolicy || effectiveLegacyAdmission === undefined) return
    const previous = effectivePolicy.legacyAdmissionEnabled
    const target = !effectiveLegacyAdmission
    setLegacyOverride({ value: target, phase: 'pending' })
    let ok = false
    if (managedPolicy) {
      ok = await managedPolicyMutation('legacy-policy', '/actions/fleet/control/policy', {
        duration_seconds: managedPolicy.durationSeconds,
        warning_after_seconds: managedPolicy.warningAfterSeconds,
        alert_after_seconds: managedPolicy.alertAfterSeconds,
        rearm_after_seconds: managedPolicy.rearmAfterSeconds,
        legacy_admission_enabled: target,
        expected_revision: managedPolicy.revision,
      })
    } else if (persistent && standaloneConfirmed) {
      ok = await mutation('legacy-policy', '/actions/persistent/policy', { legacy_admission_enabled: target })
    }
    setLegacyOverride(ok
      ? { value: target, phase: 'confirmed' }
      : { value: previous, phase: 'failed' })
  }
  const resetManagedPolicy = async () => {
    if (!managedPolicy) return
    await managedPolicyMutation('policy-reset', '/actions/fleet/control/policy/reset', { expected_revision: managedPolicy.revision })
  }
  const activeSession = selected?.workSession
  const accessReady = Boolean(selected && ((selected.access?.accessGeneration ?? 0) > 0 || loadAccessCode(selected.logicalAgentId, selected.access?.accessGeneration ?? 0)))
  const otherSlots = slots.filter(
    (slot) => slot.logicalAgentId !== selected?.logicalAgentId && slot.state !== 'deleted',
  )
  if (!instance) return meshId
    ? <FeedbackState variant="partial" title={t('title.mesh')} detail={t('slots.liveRequired')} />
    : <Navigate to="/" replace />
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
        setCreatedAccess({ logicalAgentId, displayName: publicName, code, generation })
        setCreateName('')
        setMessage(t('slots.mutationApplied'))
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
    if (!canMutate || !mutatePersistent) { setMessage(t('slots.liveRequired')); return }
    if (action === 'rotate' && !window.confirm(slot.displayName + ': ' + t('slots.rotateAccessConfirm'))) return
    const path = action === 'setup'
      ? '/actions/persistent/slots/migrate-access'
      : '/actions/persistent/slots/rotate-access-code'
    setBusy(`${slot.logicalAgentId}:access:${action}`)
    setMessage('')
    try {
      const result = await mutatePersistent(instanceId, path, { logical_agent_id: slot.logicalAgentId })
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
        setMessage(t('slots.mutationApplied'))
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
    try {
      await navigator.clipboard.writeText(code)
      setMessage(t('slots.accessCodeCopied'))
    } catch {
      setMessage(t('slots.copyFailed'))
    }
  }
  const storedAccessCode = (slot: PersistentSlotReadModel) => {
    void accessCodeRevision
    return loadAccessCode(slot.logicalAgentId, slot.access?.accessGeneration ?? 0)
  }
  const slotHref = (slot: PersistentSlotReadModel) => meshId ? meshPersistentSlotRoute(meshId, slot.logicalAgentId) : slotRoute(instanceId, slot.logicalAgentId)
  const backToSlotsHref = meshId ? meshPersistentRoute(meshId) : slotsRoute(instanceId)
  return <section className="stack" aria-labelledby="slots-title">
    <div className="page-heading"><div><p className="eyebrow">{meshId ? (managedMeshName ?? t('title.mesh')) : instance.profile.displayName}</p><h2 id="slots-title">{t('nav.slots')}</h2><p className="muted">{t('slots.description')}</p></div><div className="page-tools">{!meshId ? <label className="server-switcher"><span>{t('server.switch')}</span><select aria-label={t('aria.switchServer')} value={instanceId} onChange={(event) => navigate(slotsRoute(event.target.value))}>{instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}</select></label> : null}<span className={'status status-' + instance.runtime.status}>{localizedRuntimeState(t, instance.runtime.status)}</span></div></div>
    {instance.runtime.status !== 'live' && <div className="attention-strip" role="status">{t('slots.cachedReadOnly')} {localizedRuntimeState(t, instance.runtime.status)}.</div>}
    {persistent && !persistent.enabled && <FeedbackState variant="empty" title={t('slots.disabled')} />}
    {persistent?.enabled && !persistent.available && <FeedbackState variant="error" title={t('slots.unavailable')} detail={persistent.error ?? undefined} />}
    {message && <div className="attention-strip" role="status"><span>{message}</span></div>}
    {persistent?.enabled && loadFleetControl && !fleetControl && <div className="attention-strip" role="status">{t('slots.policyScope')}: {t('slots.policyScopeUnknown')}</div>}
    {persistent?.enabled && effectivePolicy && (persistent.policy.policyControlSupported || managedPolicy) && <article className="panel policy-controls"><div className="section-heading"><div><p className="eyebrow">{t('slots.policy')}</p><h3>{t('slots.policyControls')}</h3><p className="muted">{t('slots.policyScope')}: {managedPolicy ? `${t('slots.policyScopeManaged')}${managedMeshName ? ` · ${managedMeshName}` : ''}${fleetControlFreshness === 'stale' ? ` · ${t('connections.stale')}` : ''}` : t('slots.policyScopeLocal')}</p></div></div><p className="muted">{t('slots.policyValueHint')}</p><div className="policy-controls-grid"><label className="ui-field"><span>{t('slots.durationSeconds')}</span><input aria-label={t('slots.durationSeconds')} inputMode="text" type="text" value={draft.duration} onChange={(event) => setPolicyDraft({ ...draft, duration: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label><label className="ui-field"><span>{t('slots.warningAfterSeconds')}</span><input aria-label={t('slots.warningAfterSeconds')} inputMode="text" type="text" value={draft.warning} onChange={(event) => setPolicyDraft({ ...draft, warning: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label><label className="ui-field"><span>{t('slots.alertAfterSeconds')}</span><input aria-label={t('slots.alertAfterSeconds')} inputMode="text" type="text" value={draft.alert} onChange={(event) => setPolicyDraft({ ...draft, alert: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label><label className="ui-field"><span>{t('slots.rearmAfterSeconds')}</span><input aria-label={t('slots.rearmAfterSeconds')} inputMode="text" type="text" value={draft.rearm} onChange={(event) => setPolicyDraft({ ...draft, rearm: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label></div><div className="server-actions"><button type="button" disabled={!policyCanMutate || Boolean(busy)} onClick={() => void saveTimingPolicy()}>{t('slots.savePolicy')}</button>{managedPolicy && <button type="button" className="secondary-action" disabled={!policyCanMutate || Boolean(busy)} onClick={() => void resetManagedPolicy()}>{t('slots.resetPolicy')}</button>}</div>{!managedPolicy && timingPolicyLocked && <p className="muted">{t('slots.policyLocked')}</p>}<label className="policy-toggle"><input aria-label={t('slots.legacyToggle')} type="checkbox" checked={effectiveLegacyAdmission ?? false} disabled={!policyCanMutate || Boolean(busy)} onChange={() => void toggleLegacy()} /><span><strong>{t('slots.legacyToggle')}</strong><small>{t('slots.legacyHint')}</small></span></label>{legacyOverride ? <p className={legacyOverride.phase === 'failed' ? 'connection-error' : 'muted'} role="status">{legacyOverride.phase === 'pending' ? t('connections.pending') : legacyOverride.phase === 'confirmed' ? t('connections.confirmed') : t('connections.failed')}</p> : null}</article>}
    {persistent?.enabled && <form className="slot-create panel" onSubmit={(event) => { event.preventDefault(); const displayName = createName.trim(); if (!displayName) return; void createSlot(displayName) }}><label className="ui-field"><span>{t('slots.createName')}</span><input value={createName} onChange={(event) => setCreateName(event.target.value)} maxLength={120} /></label><button type="submit" disabled={!canMutate || busy === 'create'}>{t('slots.create')}</button>{createdAccess ? <div className="slot-access-code" role="status"><span>{createdAccess.displayName} · {t('slots.accessCodeTitle')}</span><code>{createdAccess.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + createdAccess.displayName} onClick={() => void copyAccessCode(createdAccess.code)}>{t('slots.copyAccessCode')}</button></div> : null}</form>}
    {selected && persistent && <article className="panel slot-detail" aria-label={t('title.slotDetail')}><div className="section-heading"><div><p className="eyebrow">{t('slots.accessPublicName')}</p><h3>{selected.displayName}</h3></div><span className="chip">{localizedSlotState(t, selected.state)}</span></div><div className="slot-access-code"><span>{t('slots.accessCodeTitle')}</span>{storedAccessCode(selected) ? <><code>{storedAccessCode(selected)!.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + selected.displayName} onClick={() => void copyAccessCode(storedAccessCode(selected)!.code)}>{t('slots.copyAccessCode')}</button></> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</div><details className="technical-details"><summary>{t('slots.technicalDetails')}</summary><dl className="slot-details-grid"><div><dt>{t('slots.rawState')}</dt><dd><code>{selected.state}</code></dd></div><div><dt>{t('slots.legacySelector')}</dt><dd><code>{selected.selector}</code></dd></div><div><dt>{t('slots.selectorGeneration')}</dt><dd>{selected.selectorGeneration}</dd></div><div><dt>{t('slots.authGeneration')}</dt><dd>{selected.authGeneration}</dd></div><div><dt>{t('slots.accessPublicName')}</dt><dd>{selected.access?.publicName ?? storedAccessCode(selected)?.publicName ?? selected.displayName}</dd></div><div><dt>{t('slots.accessGeneration')}</dt><dd>{selected.access?.accessGeneration ?? 0}</dd></div><div><dt>{t('slots.accessCodeTitle')}</dt><dd>{storedAccessCode(selected) ? <span className="access-code-inline"><code>{storedAccessCode(selected)?.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + selected.displayName} onClick={() => void copyAccessCode(storedAccessCode(selected)!.code)}>{t('slots.copyAccessCode')}</button></span> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</dd></div><div><dt>{t('slots.authority')}</dt><dd>{selected.authorityNodeId} · e{selected.authorityEpoch}</dd></div><div><dt>{t('slots.revision')}</dt><dd>{selected.slotRevision}</dd></div><div><dt>{t('slots.session')}</dt><dd>{activeSession ? <code>{activeSession.workSessionId}</code> : t('slots.noSession')}</dd></div><div><dt>{t('slots.sessionEpoch')}</dt><dd>{activeSession?.sessionEpoch ?? '—'}</dd></div><div><dt>{t('slots.hardExpiresAt')}</dt><dd>{activeSession?.hardExpiresAt ?? '—'}</dd></div><div><dt>{t('slots.policy')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : <>{t('slots.durationSeconds')} {duration(persistent.policy.durationSeconds)} · {t('slots.warningAfterSeconds')} {duration(persistent.policy.warningAfterSeconds)} · {t('slots.alertAfterSeconds')} {duration(persistent.policy.alertAfterSeconds)} · {t('slots.rearmAfterSeconds')} {duration(persistent.policy.rearmAfterSeconds)}</>}</dd></div><div><dt>{t('slots.manualRearm')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : persistent.policy.manualRearm ? t('slots.yes') : t('slots.no')}</dd></div><div><dt>{t('slots.admissionMode')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : persistent.policy.admissionMode}</dd></div><div><dt>{t('slots.legacyAdmission')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : persistent.policy.legacyAdmissionEnabled ? t('slots.yes') : t('slots.no')}</dd></div><div><dt>{t('slots.createdAt')}</dt><dd>{selected.createdAt}</dd></div><div><dt>{t('slots.updatedAt')}</dt><dd>{selected.updatedAt}</dd></div></dl></details><div className="server-actions">{!accessReady && <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateAccess(selected, 'setup')}>{t('slots.setupAccessCode')}</button>}{accessReady && <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateAccess(selected, 'rotate')}>{t('slots.rotateAccessCode')}</button>}</div>
      <div className="slot-subsection"><h3>{t('slots.claims')}</h3>{selected.claims.length ? selected.claims.map((claim) => <div className="slot-claim" key={`${claim.namespace}/${claim.taskId}`}><Link className="text-link" to={taskRoute(instanceId, claim.namespace, claim.taskId)}>{claim.namespace}/{claim.taskId}</Link><span>{claim.priority} · {localizedClaimState(t, claim.state)}</span>{activeSession && <div className="server-actions"><button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutation(`release:${claim.namespace}/${claim.taskId}`, '/actions/persistent/claims/release', { namespace: claim.namespace, task_id: claim.taskId, logical_agent_id: selected.logicalAgentId, work_session_id: activeSession.workSessionId, session_epoch: activeSession.sessionEpoch })}>{t('slots.releaseClaim')}</button>{otherSlots.length > 0 && <><select aria-label={t('slots.reassignTarget')} value={reassignTarget} onChange={(event) => setReassignTarget(event.target.value)}><option value="">{t('slots.chooseTarget')}</option>{otherSlots.map((slot) => <option key={slot.logicalAgentId} value={slot.logicalAgentId}>{slot.displayName}</option>)}</select><button type="button" disabled={!canMutate || !reassignTarget || Boolean(busy)} onClick={() => void mutation(`reassign:${claim.namespace}/${claim.taskId}`, '/actions/persistent/claims/reassign', { namespace: claim.namespace, task_id: claim.taskId, logical_agent_id: selected.logicalAgentId, to_logical_agent_id: reassignTarget, work_session_id: activeSession.workSessionId, session_epoch: activeSession.sessionEpoch, expected_revision: selected.slotRevision, idempotency_key: idempotencyKey() })}>{t('slots.reassignClaim')}</button></>}</div>}</div>) : <FeedbackState variant="empty" title={t('slots.noClaims')} />}</div>
      <div className="slot-subsection"><h3>{t('slots.fleet')}</h3>{selected.attachments.length ? selected.attachments.map((attachment) => <p key={attachment.nodeAttachmentId}>{attachment.nodeInstanceId} · <code>{attachment.nodeAttachmentId}</code></p>) : <FeedbackState variant="empty" title={t('slots.noAttachments')} />}</div><div className="slot-subsection"><h3>{t('slots.audit')}</h3>{selectedAudit.length ? <ul className="slot-audit">{selectedAudit.map((entry) => <li key={entry.id}><strong>{entry.eventType}</strong><span>{entry.createdAt}</span><code>{entry.principalId}</code></li>)}</ul> : <p className="muted">{t('slots.noAudit')}</p>}{auditError ? <p className="muted" role="status">{auditError}</p> : null}</div><Link className="nav-link" to={backToSlotsHref}>{t('slots.backToSlots')}</Link></article>}
    <div className="slot-grid">{slots.map((slot) => {
      const timing = slotTiming(slot, authorityNowMs, persistent?.policy.durationSeconds ?? 0, persistent?.policy.warningAfterSeconds ?? 0, persistent?.policy.alertAfterSeconds ?? 0)
      const saved = storedAccessCode(slot)
      const slotAccessReady = (slot.access?.accessGeneration ?? 0) > 0 || saved !== null
      return <article className={'panel slot-card slot-cue-' + timing.cue} key={slot.logicalAgentId}>
        <div className="section-heading"><Link className="text-link" to={slotHref(slot)}>{slot.displayName}</Link><span className="chip">{localizedSlotState(t, slot.state)}</span></div>
        {slot.workSession ? <p className="slot-time" role={timing.cue === 'normal' ? undefined : 'status'}><strong>{timing.cue === 'alert' ? t('slots.alert') : timing.cue === 'warning' ? t('slots.warning') : t('slots.time')}</strong> {timing.text}</p> : null}
        <div className="slot-access-code"><span>{t('slots.accessCodeTitle')}</span>{saved ? <><code>{saved.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + slot.displayName} onClick={() => void copyAccessCode(saved.code)}>{t('slots.copyAccessCode')}</button></> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</div>
        <div className="server-actions">
          {['suspended', 'ended', 'expired'].includes(slot.state) ? <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateSlot(slot, 'play')}>{t('slots.play')}</button> : null}
          {slot.state === 'armed' ? <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateSlot(slot, 'suspend')}>{t('slots.cancelArm')}</button> : null}
          {['active', 'stopping'].includes(slot.state) ? <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateSlot(slot, 'suspend')}>{t('slots.suspend')}</button> : null}
          <details className="slot-more-actions"><summary>{t('slots.moreActions')}</summary><div className="server-actions"><button type="button" className="secondary-action" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateAccess(slot, slotAccessReady ? 'rotate' : 'setup')}>{slotAccessReady ? t('slots.rotateAccessCode') : t('slots.setupAccessCode')}</button>
          <button type="button" className="destructive-action" disabled={!canMutate || Boolean(busy) || ['deleting', 'deleted'].includes(slot.state)} onClick={() => { if (window.confirm(slot.displayName + ': ' + t('slots.deleteConfirm'))) void mutateSlot(slot, 'delete').then((ok) => { if (ok) { clearAccessCode(slot.logicalAgentId); setAccessCodeRevision((value) => value + 1) } }) }}>{t('slots.delete')}</button>
          <Link className="nav-link" to={slotHref(slot)}>{t('slots.details')}</Link></div></details>
        </div>
      </article>
    })}{persistent?.enabled && slots.length === 0 && <FeedbackState variant="empty" title={t('slots.empty')} />}</div>
    <div className="server-actions"><Link className="nav-link" to={meshId ? meshRoute(meshId) : serverRoute(instanceId)}>{meshId ? t('title.mesh') : t('nav.backToServer')}</Link></div>
  </section>
}
