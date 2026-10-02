import { useEffect, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import type { ManagedFleetControlReadModel, ManagedFleetMutationResult, PersistentAuditReadModel, PersistentMutationResult, PersistentSlotReadModel } from '../api/models'
import { clearAccessCode, loadAccessCode, saveAccessCode } from '../access/codeVault'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { serverRoute, slotRoute, slotsRoute, taskRoute } from '../navigation/routes'
import { FeedbackState } from '../components/UiPrimitives'

export type PersistentMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>
export type FleetControlLoader = (instanceId: string) => Promise<ManagedFleetControlReadModel>
export type FleetControlMutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<ManagedFleetMutationResult>
function idempotencyKey(): string { return globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `console-${Date.now()}-${Math.random().toString(36).slice(2)}` }
function duration(seconds: number): string { const safe = Math.max(0, Math.floor(seconds)); const minutes = Math.floor(safe / 60); const rest = safe % 60; return minutes > 0 ? `${minutes}m ${rest}s` : `${rest}s` }
function slotTiming(slot: PersistentSlotReadModel, authorityNowMs: number, durationSeconds: number, warningAfter: number, alertAfter: number) { if (!slot.workSession) return { text: slot.state, cue: 'normal' as const }; const remaining = Math.max(0, Math.floor((Date.parse(slot.workSession.hardExpiresAt) - authorityNowMs) / 1000)); if (durationSeconds <= 0) return { text: duration(remaining), cue: 'normal' as const }; const elapsed = Math.max(0, durationSeconds - remaining); const cue = elapsed >= alertAfter ? 'alert' as const : elapsed >= warningAfter ? 'warning' as const : 'normal' as const; return { text: duration(remaining), cue } }

export function ServerSlots({ instances, mutatePersistent, loadSlotAudit, loadFleetControl, mutateFleetControl }: { instances: FleetInstanceView[]; mutatePersistent?: PersistentMutator; loadSlotAudit?: (instanceId: string, logicalAgentId: string) => Promise<PersistentAuditReadModel[]>; loadFleetControl?: FleetControlLoader; mutateFleetControl?: FleetControlMutator }) {
  const { t } = useI18n(); const { instanceId = '', logicalAgentId } = useParams(); const navigate = useNavigate()
  const instance = instances.find((item) => item.profile.instanceId === instanceId); const persistent = instance?.runtime.realtime?.snapshot?.persistent; const slots = persistent?.slots ?? []; const selected = logicalAgentId ? slots.find((slot) => slot.logicalAgentId === logicalAgentId) : undefined
  const [clock, setClock] = useState(() => Date.now()); const [anchor, setAnchor] = useState(() => ({ server: '', serverMs: Date.now(), localMs: Date.now() })); const [busy, setBusy] = useState(''); const [message, setMessage] = useState(''); const [createName, setCreateName] = useState(''); const [reassignTarget, setReassignTarget] = useState('')
  const [accessCodeRevision, setAccessCodeRevision] = useState(0)
  const [policyDraft, setPolicyDraft] = useState<{ key: string; duration: string; warning: string; alert: string; rearm: string } | null>(null)
  const [loadedAudit, setLoadedAudit] = useState<{ key: string; values?: PersistentAuditReadModel[]; error?: string }>({ key: '' })
  const [fleetControl, setFleetControl] = useState<ManagedFleetControlReadModel | null>(null)
  useEffect(() => {
    if (!loadFleetControl || instance?.runtime.authStatus !== 'connected') return
    let cancelled = false
    void loadFleetControl(instanceId)
      .then((control) => { if (!cancelled) setFleetControl(control) })
      .catch(() => { /* preserve last known managed policy if refresh fails */ })
    return () => { cancelled = true }
  }, [instance?.runtime.authStatus, instanceId, loadFleetControl])
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
  const effectivePolicy = managedPolicy ?? persistent?.policy
  const policyKey = effectivePolicy ? [effectivePolicy.durationSeconds, effectivePolicy.warningAfterSeconds, effectivePolicy.alertAfterSeconds, effectivePolicy.rearmAfterSeconds].join(':') : ''
  const draft = policyDraft?.key === policyKey ? policyDraft : { key: policyKey, duration: String(effectivePolicy?.durationSeconds ?? ''), warning: String(effectivePolicy?.warningAfterSeconds ?? ''), alert: String(effectivePolicy?.alertAfterSeconds ?? ''), rearm: String(effectivePolicy?.rearmAfterSeconds ?? '') }
  // Read freshness and write reachability are separate planes. A cached projection must not
  // suppress an authenticated direct-authority mutation attempt.
  const canMutate = Boolean(mutatePersistent && persistent?.enabled && instance?.runtime.authStatus === 'connected')
  const policyCanMutate = managedPolicy ? Boolean(mutateFleetControl && instance?.runtime.authStatus === 'connected') : canMutate
  const mutation = async (key: string, path: string, body: Record<string, unknown>) => { if (!canMutate || !mutatePersistent) { setMessage(t('slots.liveRequired')); return false } setBusy(key); setMessage(''); try { const result = await mutatePersistent(instanceId, path, body); if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return false } setMessage(t('slots.mutationApplied')); return true } catch (error) { setMessage(error instanceof Error ? error.message : t('slots.mutationFailed')); return false } finally { setBusy('') } }
  const managedPolicyMutation = async (key: string, path: string, body: Record<string, unknown>) => {
    if (!mutateFleetControl || instance?.runtime.authStatus !== 'connected') { setMessage(t('slots.liveRequired')); return false }
    setBusy(key); setMessage('')
    try {
      const result = await mutateFleetControl(instanceId, path, body)
      if (!result.ok) { setMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return false }
      if (result.control) setFleetControl(result.control)
      setMessage(t('slots.mutationApplied'))
      return true
    } catch (error) { setMessage(error instanceof Error ? error.message : t('slots.mutationFailed')); return false }
    finally { setBusy('') }
  }
  const mutateSlot = (slot: PersistentSlotReadModel, action: 'play' | 'suspend' | 'delete') => mutation(`${slot.logicalAgentId}:${action}`, `/actions/persistent/slots/${action}`, { logical_agent_id: slot.logicalAgentId, expected_revision: slot.slotRevision, idempotency_key: idempotencyKey() })
  const timingPolicyLocked = slots.some((slot) => ['armed', 'active', 'stopping'].includes(slot.state))
  const saveTimingPolicy = async () => {
    const durationSeconds = Number(draft.duration); const warningAfterSeconds = Number(draft.warning); const alertAfterSeconds = Number(draft.alert); const rearmAfterSeconds = Number(draft.rearm)
    if (![durationSeconds, warningAfterSeconds, alertAfterSeconds, rearmAfterSeconds].every(Number.isInteger) || !(0 < warningAfterSeconds && warningAfterSeconds < alertAfterSeconds && alertAfterSeconds < durationSeconds) || rearmAfterSeconds <= 0) {
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
    if (!effectivePolicy) return
    if (managedPolicy) {
      await managedPolicyMutation('legacy-policy', '/actions/fleet/control/policy', {
        duration_seconds: managedPolicy.durationSeconds,
        warning_after_seconds: managedPolicy.warningAfterSeconds,
        alert_after_seconds: managedPolicy.alertAfterSeconds,
        rearm_after_seconds: managedPolicy.rearmAfterSeconds,
        legacy_admission_enabled: !managedPolicy.legacyAdmissionEnabled,
        expected_revision: managedPolicy.revision,
      })
      return
    }
    if (!persistent) return
    await mutation('legacy-policy', '/actions/persistent/policy', { legacy_admission_enabled: !persistent.policy.legacyAdmissionEnabled })
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
  if (!instance) return <Navigate to="/" replace />
  const mutateAccess = async (slot: PersistentSlotReadModel, action: 'setup' | 'rotate') => {
    if (!canMutate || !mutatePersistent) { setMessage(t('slots.liveRequired')); return }
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
  return <section className="stack" aria-labelledby="slots-title">
    <div className="page-heading"><div><p className="eyebrow">{instance.profile.displayName}</p><h2 id="slots-title">{t('nav.slots')}</h2><p className="muted">{t('slots.description')}</p></div><div className="page-tools"><label className="server-switcher"><span>{t('server.switch')}</span><select aria-label={t('aria.switchServer')} value={instanceId} onChange={(event) => navigate(slotsRoute(event.target.value))}>{instances.map((item) => <option key={item.profile.instanceId} value={item.profile.instanceId}>{item.profile.displayName}</option>)}</select></label><span className={'status status-' + instance.runtime.status}>{instance.runtime.status}</span></div></div>
    {instance.runtime.status !== 'live' && <div className="attention-strip" role="status">{t('slots.cachedReadOnly')} {instance.runtime.status}.</div>}
    {persistent && !persistent.enabled && <FeedbackState variant="empty" title={t('slots.disabled')} />}
    {persistent?.enabled && !persistent.available && <FeedbackState variant="error" title={t('slots.unavailable')} detail={persistent.error ?? undefined} />}
    {message && <div className="attention-strip" role="status"><span>{message}</span></div>}
    {persistent?.enabled && (persistent.policy.policyControlSupported || managedPolicy) && <article className="panel policy-controls"><div className="section-heading"><div><p className="eyebrow">{t('slots.policy')}</p><h3>{t('slots.policyControls')}</h3></div></div><div className="policy-controls-grid"><label className="ui-field"><span>{t('slots.durationSeconds')}</span><input aria-label={t('slots.durationSeconds')} inputMode="numeric" type="number" min="1" value={draft.duration} onChange={(event) => setPolicyDraft({ ...draft, duration: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label><label className="ui-field"><span>{t('slots.warningAfterSeconds')}</span><input aria-label={t('slots.warningAfterSeconds')} inputMode="numeric" type="number" min="1" value={draft.warning} onChange={(event) => setPolicyDraft({ ...draft, warning: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label><label className="ui-field"><span>{t('slots.alertAfterSeconds')}</span><input aria-label={t('slots.alertAfterSeconds')} inputMode="numeric" type="number" min="1" value={draft.alert} onChange={(event) => setPolicyDraft({ ...draft, alert: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label><label className="ui-field"><span>{t('slots.rearmAfterSeconds')}</span><input aria-label={t('slots.rearmAfterSeconds')} inputMode="numeric" type="number" min="1" value={draft.rearm} onChange={(event) => setPolicyDraft({ ...draft, rearm: event.target.value })} disabled={!policyCanMutate || Boolean(busy)} /></label></div><div className="server-actions"><button type="button" disabled={!policyCanMutate || Boolean(busy)} onClick={() => void saveTimingPolicy()}>{t('slots.savePolicy')}</button>{managedPolicy && <button type="button" className="secondary-action" disabled={!policyCanMutate || Boolean(busy)} onClick={() => void resetManagedPolicy()}>{t('slots.resetPolicy')}</button>}</div>{!managedPolicy && timingPolicyLocked && <p className="muted">{t('slots.policyLocked')}</p>}<label className="policy-toggle"><input aria-label={t('slots.legacyToggle')} type="checkbox" checked={effectivePolicy?.legacyAdmissionEnabled ?? false} disabled={!policyCanMutate || Boolean(busy)} onChange={() => void toggleLegacy()} /><span><strong>{t('slots.legacyToggle')}</strong><small>{t('slots.legacyHint')}</small></span></label></article>}
    {persistent?.enabled && <form className="slot-create panel" onSubmit={(event) => { event.preventDefault(); const displayName = createName.trim(); if (!displayName) return; void mutation('create', '/actions/persistent/slots/create', { display_name: displayName }).then((ok) => { if (ok) setCreateName('') }) }}><label className="ui-field"><span>{t('slots.createName')}</span><input value={createName} onChange={(event) => setCreateName(event.target.value)} maxLength={120} /></label><button type="submit" disabled={!canMutate || busy === 'create'}>{t('slots.create')}</button></form>}
    {selected && persistent && <article className="panel slot-detail" aria-label={t('title.slotDetail')}><div className="section-heading"><div><p className="eyebrow">{t('slots.accessPublicName')}</p><h3>{selected.displayName}</h3></div><span className="chip">{selected.state}</span></div><dl className="slot-details-grid"><div><dt>{t('slots.legacySelector')}</dt><dd><code>{selected.selector}</code></dd></div><div><dt>{t('slots.selectorGeneration')}</dt><dd>{selected.selectorGeneration}</dd></div><div><dt>{t('slots.authGeneration')}</dt><dd>{selected.authGeneration}</dd></div><div><dt>{t('slots.accessPublicName')}</dt><dd>{selected.access?.publicName ?? storedAccessCode(selected)?.publicName ?? selected.displayName}</dd></div><div><dt>{t('slots.accessGeneration')}</dt><dd>{selected.access?.accessGeneration ?? 0}</dd></div><div><dt>{t('slots.accessCodeTitle')}</dt><dd>{storedAccessCode(selected) ? <span className="access-code-inline"><code>{storedAccessCode(selected)?.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + selected.displayName} onClick={() => void copyAccessCode(storedAccessCode(selected)!.code)}>{t('slots.copyAccessCode')}</button></span> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</dd></div><div><dt>{t('slots.authority')}</dt><dd>{selected.authorityNodeId} · e{selected.authorityEpoch}</dd></div><div><dt>{t('slots.revision')}</dt><dd>{selected.slotRevision}</dd></div><div><dt>{t('slots.session')}</dt><dd>{activeSession ? <code>{activeSession.workSessionId}</code> : t('slots.noSession')}</dd></div><div><dt>{t('slots.sessionEpoch')}</dt><dd>{activeSession?.sessionEpoch ?? '—'}</dd></div><div><dt>{t('slots.hardExpiresAt')}</dt><dd>{activeSession?.hardExpiresAt ?? '—'}</dd></div><div><dt>{t('slots.policy')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : <>D {duration(persistent.policy.durationSeconds)} · W {duration(persistent.policy.warningAfterSeconds)} · A {duration(persistent.policy.alertAfterSeconds)} · R {duration(persistent.policy.rearmAfterSeconds)}</>}</dd></div><div><dt>{t('slots.manualRearm')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : persistent.policy.manualRearm ? t('slots.yes') : t('slots.no')}</dd></div><div><dt>{t('slots.admissionMode')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : persistent.policy.admissionMode}</dd></div><div><dt>{t('slots.legacyAdmission')}</dt><dd>{persistent.policy.projected ? t('common.unavailable') : persistent.policy.legacyAdmissionEnabled ? t('slots.yes') : t('slots.no')}</dd></div><div><dt>{t('slots.createdAt')}</dt><dd>{selected.createdAt}</dd></div><div><dt>{t('slots.updatedAt')}</dt><dd>{selected.updatedAt}</dd></div></dl><div className="server-actions">{!accessReady && <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateAccess(selected, 'setup')}>{t('slots.setupAccessCode')}</button>}{accessReady && <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateAccess(selected, 'rotate')}>{t('slots.rotateAccessCode')}</button>}</div>
      <div className="slot-subsection"><h3>{t('slots.claims')}</h3>{selected.claims.length ? selected.claims.map((claim) => <div className="slot-claim" key={`${claim.namespace}/${claim.taskId}`}><Link className="text-link" to={taskRoute(instanceId, claim.namespace, claim.taskId)}>{claim.namespace}/{claim.taskId}</Link><span>{claim.priority} · {claim.state}</span>{activeSession && <div className="server-actions"><button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutation(`release:${claim.namespace}/${claim.taskId}`, '/actions/persistent/claims/release', { namespace: claim.namespace, task_id: claim.taskId, logical_agent_id: selected.logicalAgentId, work_session_id: activeSession.workSessionId, session_epoch: activeSession.sessionEpoch })}>{t('slots.releaseClaim')}</button>{otherSlots.length > 0 && <><select aria-label={t('slots.reassignTarget')} value={reassignTarget} onChange={(event) => setReassignTarget(event.target.value)}><option value="">{t('slots.chooseTarget')}</option>{otherSlots.map((slot) => <option key={slot.logicalAgentId} value={slot.logicalAgentId}>{slot.displayName}</option>)}</select><button type="button" disabled={!canMutate || !reassignTarget || Boolean(busy)} onClick={() => void mutation(`reassign:${claim.namespace}/${claim.taskId}`, '/actions/persistent/claims/reassign', { namespace: claim.namespace, task_id: claim.taskId, logical_agent_id: selected.logicalAgentId, to_logical_agent_id: reassignTarget, work_session_id: activeSession.workSessionId, session_epoch: activeSession.sessionEpoch, expected_revision: selected.slotRevision, idempotency_key: idempotencyKey() })}>{t('slots.reassignClaim')}</button></>}</div>}</div>) : <p className="muted">{t('slots.noClaims')}</p>}</div>
      <div className="slot-subsection"><h3>{t('slots.fleet')}</h3>{selected.attachments.length ? selected.attachments.map((attachment) => <p key={attachment.nodeAttachmentId}>{attachment.nodeInstanceId} · <code>{attachment.nodeAttachmentId}</code></p>) : <p className="muted">{t('slots.noAttachments')}</p>}</div><div className="slot-subsection"><h3>{t('slots.audit')}</h3>{selectedAudit.length ? <ul className="slot-audit">{selectedAudit.map((entry) => <li key={entry.id}><strong>{entry.eventType}</strong><span>{entry.createdAt}</span><code>{entry.principalId}</code></li>)}</ul> : <p className="muted">{t('slots.noAudit')}</p>}{auditError ? <p className="muted" role="status">{auditError}</p> : null}</div><Link className="nav-link" to={slotsRoute(instanceId)}>{t('slots.backToSlots')}</Link></article>}
    <div className="slot-grid">{slots.map((slot) => {
      const timing = slotTiming(slot, authorityNowMs, persistent?.policy.durationSeconds ?? 0, persistent?.policy.warningAfterSeconds ?? 0, persistent?.policy.alertAfterSeconds ?? 0)
      const saved = storedAccessCode(slot)
      const slotAccessReady = (slot.access?.accessGeneration ?? 0) > 0 || saved !== null
      return <article className={'panel slot-card slot-cue-' + timing.cue} key={slot.logicalAgentId}>
        <div className="section-heading"><Link className="text-link" to={slotRoute(instanceId, slot.logicalAgentId)}>{slot.displayName}</Link><span className="chip">{slot.state}</span></div>
        <p className="slot-time" role={timing.cue === 'normal' ? undefined : 'status'}><strong>{timing.cue === 'alert' ? t('slots.alert') : timing.cue === 'warning' ? t('slots.warning') : t('slots.time')}</strong> {timing.text}</p>
        <div className="slot-access-code"><span>{t('slots.accessCodeTitle')}</span>{saved ? <><code>{saved.code}</code><button type="button" className="secondary-action" aria-label={t('slots.copyAccessCode') + ' — ' + slot.displayName} onClick={() => void copyAccessCode(saved.code)}>{t('slots.copyAccessCode')}</button></> : <span className="muted">{t('slots.accessCodeUnavailable')}</span>}</div>
        <div className="server-actions">
          <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateSlot(slot, 'play')}>{t('slots.play')}</button>
          <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateSlot(slot, 'suspend')}>{slot.state === 'armed' ? t('slots.cancelArm') : t('slots.suspend')}</button>
          <button type="button" disabled={!canMutate || Boolean(busy)} onClick={() => void mutateAccess(slot, slotAccessReady ? 'rotate' : 'setup')}>{slotAccessReady ? t('slots.rotateAccessCode') : t('slots.setupAccessCode')}</button>
          <button type="button" className="destructive-action" disabled={!canMutate || Boolean(busy)} onClick={() => { if (window.confirm(t('slots.deleteConfirm'))) void mutateSlot(slot, 'delete').then((ok) => { if (ok) { clearAccessCode(slot.logicalAgentId); setAccessCodeRevision((value) => value + 1) } }) }}>{t('slots.delete')}</button>
          <Link className="nav-link" to={slotRoute(instanceId, slot.logicalAgentId)}>{t('slots.details')}</Link>
        </div>
      </article>
    })}{persistent?.enabled && slots.length === 0 && <FeedbackState variant="empty" title={t('slots.empty')} />}</div>
    <div className="server-actions"><Link className="nav-link" to={serverRoute(instanceId)}>{t('nav.backToServer')}</Link></div>
  </section>
}
