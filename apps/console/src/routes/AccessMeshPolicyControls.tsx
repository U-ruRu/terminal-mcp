import { ConsoleHttpError } from '../api/client'
import { useRef, useState } from 'react'

import type { AccessMeshDefaultsReadModel, AccessMeshMutationRequest, AccessMeshPolicyReadModel, AccessMeshSlotReadModel, AccessMutationResult } from '../api/models'
import { ConfirmationDialog, IconButton, IconButtonRow } from '../components/UiPrimitives'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { DurationField } from '../slots/DurationField'
import { durationSecondsValue, durationValue, type DurationValue } from '../slots/duration'
import { newSlotOperationKey } from '../slots/operationJournal'
import { PolicyInheritance, type PolicyInheritanceDisplay } from '../slots/PolicyInheritance'

export type AccessMeshActionResult = { result: AccessMutationResult; confirmed: boolean }
export type AccessMeshActionExecutor = (request: AccessMeshMutationRequest) => Promise<AccessMeshActionResult>
type PolicyDraft = {
  revision: number
  base: string
  duration: DurationValue
  warning: DurationValue
  draining: DurationValue
  rearm: DurationValue
  rearmEnabled: boolean
  legacyAdmissionEnabled: boolean
  releaseOnEnd: 'mode' | 'yes' | 'no'
}
type NativeSlot = AccessMeshSlotReadModel & { inheritance?: PolicyInheritanceDisplay; windowPolicy?: AccessMeshPolicyReadModel | null }

type FormProps = {
  policy: AccessMeshPolicyReadModel
  legacyAdmissionEnabled?: boolean
  revision: number
  inheritance?: PolicyInheritanceDisplay
  slotId?: string
  policySupported: boolean
  canMutate: boolean
  externalBusy?: boolean
  execute: AccessMeshActionExecutor
  refresh: () => void
}

function policyKey(props: FormProps): string {
  return JSON.stringify([props.revision, props.policy, props.legacyAdmissionEnabled, props.inheritance?.parent?.version ?? '', props.inheritance?.mode ?? ''])
}

function makeDraft(props: FormProps): PolicyDraft {
  return {
    revision: props.revision, base: policyKey(props),
    duration: durationValue(props.policy.durationSeconds), warning: durationValue(props.policy.warningSeconds),
    draining: durationValue(props.policy.drainingSeconds), rearm: durationValue(props.policy.cooldownSeconds),
    rearmEnabled: props.policy.rearmEnabled,
    legacyAdmissionEnabled: props.legacyAdmissionEnabled === true,
    releaseOnEnd: props.policy.releaseOnEnd === null ? 'mode' : props.policy.releaseOnEnd ? 'yes' : 'no',
  }
}

function NativePolicyForm(props: FormProps) {
  const { t } = useI18n()
  const [edited, setEdited] = useState<PolicyDraft | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [resetConfirmation, setResetConfirmation] = useState<{ revision: number; parentRevision?: number | null } | null>(null)
  const draft = edited ?? makeDraft(props)
  const conflict = Boolean(edited && edited.base !== policyKey(props))
  const disabled = !props.policySupported || !props.canMutate || Boolean(props.externalBusy) || busy

  const perform = async (request: AccessMeshMutationRequest) => {
    if (disabled || busyRef.current) return
    busyRef.current = true; setBusy(true); setNotice('')
    try {
      const outcome = await props.execute(request)
      if (outcome.result.ok && outcome.confirmed) { setEdited(null); setNotice(t('slots.managed.confirmed')) }
      else if (outcome.result.ok) setNotice(t('slots.managed.confirmationPending'))
      else setNotice(t(outcome.result.code?.includes('conflict') ? 'slots.managed.conflict' : 'slots.managed.saveFailed'))
    } catch (error) { setNotice(t(error instanceof ConsoleHttpError && error.status === 409 ? 'slots.managed.conflict' : 'slots.managed.uncertain')) }
    finally { busyRef.current = false; setBusy(false) }
  }

  const save = () => {
    if (conflict) return
    const duration = durationSecondsValue(draft.duration), warning = durationSecondsValue(draft.warning)
    const draining = durationSecondsValue(draft.draining), rearm = durationSecondsValue(draft.rearm)
    // Native Access Mesh requires both before-expiry thresholds < duration; no relative order is imposed.
    if (duration === null || duration <= 0 || warning === null || draining === null || rearm === null || warning >= duration || draining >= duration) {
      setNotice(t('slots.native.invalidPolicy')); return
    }
    const policy = { duration_seconds: duration, warning_seconds: warning, draining_seconds: draining, cooldown_seconds: rearm, rearm_enabled: draft.rearmEnabled, release_on_end: draft.releaseOnEnd === 'mode' ? null : draft.releaseOnEnd === 'yes' }
    void perform(props.slotId
      ? { action: 'policy', slot_id: props.slotId, expected_revision: draft.revision, policy, idempotency_key: newSlotOperationKey() }
      : { action: 'defaults', expected_revision: draft.revision, policy, legacy_admission_enabled: draft.legacyAdmissionEnabled, idempotency_key: newSlotOperationKey() })
  }

  const reset = () => {
    const confirmation = resetConfirmation
    setResetConfirmation(null)
    if (!confirmation || !props.inheritance?.resetSupported) return
    if (confirmation.revision !== props.revision || props.slotId && confirmation.parentRevision !== props.inheritance.parent?.revision) { setNotice(t('slots.managed.conflict')); return }
    if (props.slotId && (!Number.isSafeInteger(confirmation.parentRevision) || Number(confirmation.parentRevision) < 1)) { setNotice(t('slots.managed.conflict')); return }
    // Inheritance is a server operation, never a client-side copy of parent values.
    void perform(props.slotId
      ? { action: 'policy', slot_id: props.slotId, expected_revision: confirmation.revision, expected_parent_revision: Number(confirmation.parentRevision), inherit_policy: true, idempotency_key: newSlotOperationKey() }
      : { action: 'defaults', expected_revision: confirmation.revision, inherit_policy: true, idempotency_key: newSlotOperationKey() })
  }

  return <>
    <div className="section-heading"><h3>{t(props.slotId ? 'slots.managed.title' : 'slots.policyControls')}</h3><IconButton icon="retry" variant="quiet" label={t('slots.managed.refresh')} disabled={busy || Boolean(props.externalBusy)} onClick={props.refresh} /></div>
    <PolicyInheritance value={props.inheritance} />
    {!props.policySupported ? <p role="status" className="attention-strip">{t('slots.native.policyUpgradeRequired')}</p> : null}
    <p className="muted">{t(props.policySupported && props.inheritance?.appliesTo === 'next_window' ? 'slots.managed.nextWindow' : 'slots.inheritance.currentSchedule')}</p>
    <div className="policy-controls-grid">
      <DurationField label={t(props.slotId ? 'slots.managed.duration' : 'slots.durationSeconds')} value={draft.duration} disabled={disabled} onChange={(duration) => setEdited({ ...draft, duration })} />
      <DurationField label={t('slots.managed.warning')} value={draft.warning} disabled={disabled} onChange={(warning) => setEdited({ ...draft, warning })} />
      <DurationField label={t('slots.managed.draining')} value={draft.draining} disabled={disabled} onChange={(draining) => setEdited({ ...draft, draining })} />
      <DurationField label={t('slots.managed.rearm')} value={draft.rearm} disabled={disabled} onChange={(rearm) => setEdited({ ...draft, rearm })} />
    </div>
    <label className="policy-toggle"><input type="checkbox" checked={draft.rearmEnabled} disabled={disabled} onChange={(event) => setEdited({ ...draft, rearmEnabled: event.target.checked })} /><span>{t('slots.native.rearmEnabled')}</span></label>
    {!props.slotId ? <><label className="policy-toggle"><input type="checkbox" checked={draft.legacyAdmissionEnabled} disabled={disabled} onChange={(event) => setEdited({ ...draft, legacyAdmissionEnabled: event.target.checked })} /><span>{t('slots.legacyAdmission')}</span></label><p className="muted">{t('slots.legacyAdmissionHint')}</p></> : null}
    <label className="ui-field"><span>{t('slots.native.releaseOnEnd')}</span><select aria-label={t('slots.native.releaseOnEnd')} value={draft.releaseOnEnd} disabled={disabled} onChange={(event) => setEdited({ ...draft, releaseOnEnd: event.target.value as PolicyDraft['releaseOnEnd'] })}><option value="mode">{t('slots.native.modeDefault')}</option><option value="yes">{t('slots.yes')}</option><option value="no">{t('slots.no')}</option></select></label>

    {conflict ? <p role="alert" className="attention-strip">{t('slots.managed.conflict')}</p> : null}
    {notice ? <p role="status" className="attention-strip">{notice}</p> : null}
    <IconButtonRow className="server-actions policy-actions">
      <IconButton icon="save" variant="primary" label={t(props.slotId ? 'slots.managed.savePolicy' : 'slots.savePolicy')} disabled={disabled || conflict} busy={busy} onClick={save} />
      <IconButton icon="reset" variant="secondary" label={t('slots.managed.cancel')} disabled={busy || !edited} onClick={() => { setEdited(null); setNotice('') }} />
      {props.policySupported && props.inheritance?.resetSupported && props.inheritance.mode === 'override' ? <IconButton icon="reset" variant="secondary" label={t('slots.inheritance.reset')} disabled={disabled} onClick={() => setResetConfirmation({ revision: props.revision, parentRevision: props.inheritance?.parent?.revision })} /> : null}
    </IconButtonRow>
    {resetConfirmation ? <ConfirmationDialog title={t('slots.inheritance.reset')} subject={props.inheritance?.parent?.id ?? ''} detail={t('slots.inheritance.resetConfirm')} cancelLabel={t('slots.confirmCancel')} confirmLabel={t('slots.inheritance.reset')} confirmIcon="reset" confirmVariant="primary" onCancel={() => setResetConfirmation(null)} onConfirm={reset} /> : null}
  </>
}

export function AccessMeshDefaultsControls({ defaults, ...props }: {
  defaults: AccessMeshDefaultsReadModel & { inheritance?: PolicyInheritanceDisplay }
  canMutate: boolean
  externalBusy?: boolean
  execute: AccessMeshActionExecutor
  refresh: () => void
}) {
  return <div className="policy-controls"><NativePolicyForm {...props} policy={defaults.policy} legacyAdmissionEnabled={defaults.legacyAdmissionEnabled} revision={defaults.revision} inheritance={defaults.inheritance} policySupported={Boolean(defaults.managementContractVersion === 2 && defaults.inheritanceSupported && defaults.inheritance?.appliesTo === 'next_window')} /></div>
}

export function AccessMeshSlotControls({ slot, canMutate, externalBusy, execute, refresh }: {
  slot: NativeSlot
  canMutate: boolean
  externalBusy?: boolean
  execute: AccessMeshActionExecutor
  refresh: () => void
}) {
  const { t, locale } = useI18n()
  const [windowDraft, setWindowDraft] = useState<{ revision: number; startedAt: string; duration: DurationValue } | null>(null)
  const [pendingDeadline, setPendingDeadline] = useState<AccessMeshMutationRequest | null>(null)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [notice, setNotice] = useState('')
  const cycle = slot.sessionLifecycle
  const startedAt = cycle.startedAt ?? ''
  const durationSeconds = startedAt && cycle.hardExpiresAt ? (Date.parse(cycle.hardExpiresAt) - Date.parse(startedAt)) / 1000 : null
  const duration = windowDraft?.duration ?? durationValue(durationSeconds ?? 0)
  const active = slot.managementContractVersion === 2 && ['active', 'warning', 'draining'].includes(cycle.state) && durationSeconds !== null
  const conflict = Boolean(windowDraft && (windowDraft.revision !== slot.revision || windowDraft.startedAt !== startedAt))
  const disabled = !canMutate || Boolean(externalBusy) || busy

  const confirmWindow = () => {
    if (disabled || !active) return
    const seconds = durationSecondsValue(duration)
    const deadline = seconds === null ? NaN : Date.parse(startedAt) + seconds * 1000
    if (seconds === null || seconds <= 0 || !Number.isFinite(deadline) || Math.abs(deadline) > 8640000000000000) { setNotice(t('slots.native.invalidPolicy')); return }
    if (deadline < Date.parse(cycle.hardExpiresAt ?? '')) { setNotice(t('slots.native.windowExtensionOnly')); return }
    if (conflict) return
    setPendingDeadline({ action: 'deadline', slot_id: slot.slotId, expected_revision: windowDraft?.revision ?? slot.revision, deadline_at: new Date(deadline).toISOString(), idempotency_key: newSlotOperationKey() })
  }
  const applyWindow = async () => {
    const request = pendingDeadline
    setPendingDeadline(null)
    if (!request || disabled || busyRef.current) return
    busyRef.current = true; setBusy(true); setNotice('')
    try {
      const outcome = await execute(request)
      if (outcome.result.ok && outcome.confirmed) { setWindowDraft(null); setNotice(t('slots.managed.confirmed')) }
      else setNotice(t(outcome.result.ok ? 'slots.managed.confirmationPending' : 'slots.managed.saveFailed'))
    } catch (error) { setNotice(t(error instanceof ConsoleHttpError && error.status === 409 ? 'slots.managed.conflict' : 'slots.managed.uncertain')) }
    finally { busyRef.current = false; setBusy(false) }
  }
  return <div className="slot-subsection slot-policy-controls" aria-label={t('slots.managed.title')}>
    <NativePolicyForm policySupported={Boolean(slot.managementContractVersion === 2 && slot.inheritanceSupported && slot.inheritance?.appliesTo === 'next_window')} policy={slot.policy} revision={slot.revision} inheritance={slot.inheritance} slotId={slot.slotId} canMutate={canMutate} externalBusy={externalBusy || busy} execute={execute} refresh={refresh} />
    <div className="slot-subsection"><h3>{t('slots.managed.window')}</h3>
      {slot.managementContractVersion !== 2 ? <p className="muted">{t('slots.native.sessionStateUnverified')}</p> : cycle.hardExpiresAt ? <dl className="slot-details-grid"><div><dt>{t('slots.managed.opened')}</dt><dd>{cycle.startedAt ?? t('common.unavailable')}</dd></div><div><dt>{t('slots.managed.deadline')}</dt><dd>{cycle.hardExpiresAt}</dd></div><div><dt>{t('slots.managed.remaining')}</dt><dd>{cycle.remainingSeconds === undefined ? t('common.unavailable') : formatDuration(cycle.remainingSeconds, locale)}</dd></div>
        {slot.windowPolicy ? <><div><dt>{t('slots.managed.duration')}</dt><dd>{formatDuration(slot.windowPolicy.durationSeconds, locale)}</dd></div><div><dt>{t('slots.managed.windowWarning')}</dt><dd>{formatDuration(slot.windowPolicy.warningSeconds, locale)}</dd></div><div><dt>{t('slots.managed.windowDraining')}</dt><dd>{formatDuration(slot.windowPolicy.drainingSeconds, locale)}</dd></div></> : null}
      </dl> : <p className="muted">{t('slots.managed.noWindow')}</p>}
      {active ? <><p className="muted">{t('slots.managed.windowHint')}</p><DurationField label={t('slots.managed.windowDuration')} value={duration} disabled={disabled} onChange={(value) => setWindowDraft({ revision: windowDraft?.revision ?? slot.revision, startedAt: windowDraft?.startedAt ?? startedAt, duration: value })} />
        {conflict ? <p role="alert" className="attention-strip">{t('slots.managed.conflict')}</p> : null}
        <IconButtonRow className="server-actions policy-actions"><IconButton icon="save" variant="primary" label={t('slots.managed.saveWindow')} disabled={disabled || conflict} onClick={confirmWindow} /><IconButton icon="reset" variant="secondary" label={t('slots.managed.cancelWindow')} disabled={disabled || !windowDraft} onClick={() => { setWindowDraft(null); setNotice('') }} /></IconButtonRow>
      </> : null}
      {notice ? <p role="status" className="attention-strip">{notice}</p> : null}
      {pendingDeadline ? <ConfirmationDialog title={t('slots.managed.windowConfirm')} subject={slot.publicName} detail={t('slots.managed.windowHint')} cancelLabel={t('slots.confirmCancel')} confirmLabel={t('slots.managed.applyWindow')} confirmIcon="save" confirmVariant="primary" onCancel={() => setPendingDeadline(null)} onConfirm={() => void applyWindow()} /> : null}
    </div>
  </div>
}
