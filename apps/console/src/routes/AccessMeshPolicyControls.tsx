import { useRef, useState } from 'react'

import type { AccessMeshDefaultsReadModel, AccessMeshMutationRequest, AccessMeshPolicyReadModel, AccessMeshSlotReadModel, AccessMutationResult } from '../api/models'
import { ConfirmationDialog, IconButton, IconButtonRow } from '../components/UiPrimitives'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { DurationField } from '../slots/DurationField'
import { durationSecondsValue, durationValue, type DurationValue } from '../slots/duration'
import { newSlotOperationKey } from '../slots/operationJournal'
import type { PolicyInheritanceDisplay } from '../slots/PolicyInheritance'

export type AccessMeshActionResult = {
  result: AccessMutationResult
  confirmed: boolean
  savedPolicy?: { policy: AccessMeshPolicyReadModel; revision: number; legacyAdmissionEnabled?: boolean }
}
export type AccessMeshActionExecutor = (request: AccessMeshMutationRequest) => Promise<AccessMeshActionResult>
type PolicyDraft = {
  revision: number
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
  notify: (message: string) => void
}

function makeDraft(props: Pick<FormProps, 'policy' | 'revision' | 'legacyAdmissionEnabled'>): PolicyDraft {
  return {
    revision: props.revision,
    duration: durationValue(props.policy.durationSeconds), warning: durationValue(props.policy.warningSeconds),
    draining: durationValue(props.policy.drainingSeconds), rearm: durationValue(props.policy.cooldownSeconds),
    rearmEnabled: props.policy.rearmEnabled,
    legacyAdmissionEnabled: props.legacyAdmissionEnabled === true,
    releaseOnEnd: props.policy.releaseOnEnd === null ? 'mode' : props.policy.releaseOnEnd ? 'yes' : 'no',
  }
}

function NativePolicyForm(props: FormProps) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(() => makeDraft(props))
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [resetConfirmation, setResetConfirmation] = useState(false)
  const disabled = !props.policySupported || !props.canMutate || Boolean(props.externalBusy) || busy

  const perform = async (request: AccessMeshMutationRequest) => {
    if (disabled || busyRef.current) return
    busyRef.current = true; setBusy(true)
    try {
      const outcome = await props.execute(request)
      if (outcome.result.ok && outcome.confirmed && outcome.savedPolicy) setDraft(makeDraft(outcome.savedPolicy))
    } catch { /* The action owner reports failures through the shared toast. */ }
    finally { busyRef.current = false; setBusy(false) }
  }

  const save = () => {
    const duration = durationSecondsValue(draft.duration), warning = durationSecondsValue(draft.warning)
    const draining = durationSecondsValue(draft.draining), rearm = durationSecondsValue(draft.rearm)
    // Native Access Mesh requires both before-expiry thresholds < duration; no relative order is imposed.
    if (duration === null || duration <= 0 || warning === null || draining === null || rearm === null || warning >= duration || draining >= duration) {
      props.notify(t('slots.native.invalidPolicy')); return
    }
    const policy = { duration_seconds: duration, warning_seconds: warning, draining_seconds: draining, cooldown_seconds: rearm, rearm_enabled: draft.rearmEnabled, release_on_end: draft.releaseOnEnd === 'mode' ? null : draft.releaseOnEnd === 'yes' }
    void perform(props.slotId
      ? { action: 'policy', slot_id: props.slotId, expected_revision: draft.revision, policy, idempotency_key: newSlotOperationKey() }
      : { action: 'defaults', expected_revision: draft.revision, policy, legacy_admission_enabled: draft.legacyAdmissionEnabled, idempotency_key: newSlotOperationKey() })
  }

  const reset = () => {
    setResetConfirmation(false)
    if (!resetConfirmation || !props.inheritance?.resetSupported) return
    // Inheritance is a server operation, never a client-side copy of parent values.
    void perform(props.slotId
      ? { action: 'policy', slot_id: props.slotId, expected_revision: props.revision, expected_parent_revision: props.inheritance.parent?.revision ?? undefined, inherit_policy: true, idempotency_key: newSlotOperationKey() }
      : { action: 'defaults', expected_revision: props.revision, inherit_policy: true, idempotency_key: newSlotOperationKey() })
  }

  return <>
    <div className="section-heading"><h3>{t(props.slotId ? 'slots.managed.title' : 'slots.policyControls')}</h3><IconButton icon="retry" variant="quiet" label={t('slots.managed.refresh')} disabled={busy || Boolean(props.externalBusy)} onClick={props.refresh} /></div>
    <div className="policy-controls-grid">
      <DurationField label={t(props.slotId ? 'slots.managed.duration' : 'slots.durationSeconds')} value={draft.duration} disabled={busy} onChange={(duration) => setDraft({ ...draft, duration })} />
      <DurationField label={t('slots.managed.warning')} value={draft.warning} disabled={busy} onChange={(warning) => setDraft({ ...draft, warning })} />
      <DurationField label={t('slots.managed.draining')} value={draft.draining} disabled={busy} onChange={(draining) => setDraft({ ...draft, draining })} />
      <DurationField label={t('slots.managed.rearm')} value={draft.rearm} disabled={busy} onChange={(rearm) => setDraft({ ...draft, rearm })} />
    </div>
    <label className="policy-toggle"><input type="checkbox" checked={draft.rearmEnabled} disabled={busy} onChange={(event) => setDraft({ ...draft, rearmEnabled: event.target.checked })} /><span>{t('slots.native.rearmEnabled')}</span></label>
    {!props.slotId ? <label className="policy-toggle"><input type="checkbox" checked={draft.legacyAdmissionEnabled} disabled={busy} onChange={(event) => setDraft({ ...draft, legacyAdmissionEnabled: event.target.checked })} /><span>{t('slots.legacyAdmission')}</span></label> : null}
    <label className="ui-field"><span>{t('slots.native.releaseOnEnd')}</span><select aria-label={t('slots.native.releaseOnEnd')} value={draft.releaseOnEnd} disabled={busy} onChange={(event) => setDraft({ ...draft, releaseOnEnd: event.target.value as PolicyDraft['releaseOnEnd'] })}><option value="mode">{t('slots.native.modeDefault')}</option><option value="yes">{t('slots.yes')}</option><option value="no">{t('slots.no')}</option></select></label>

    <IconButtonRow className="server-actions policy-actions">
      <IconButton icon="save" variant="primary" label={t(props.slotId ? 'slots.managed.savePolicy' : 'slots.savePolicy')} disabled={disabled} busy={busy} onClick={save} />
      <IconButton icon="reset" variant="secondary" label={t('slots.managed.cancel')} disabled={busy} onClick={() => setDraft(makeDraft(props))} />
      {props.policySupported && props.inheritance?.resetSupported && props.inheritance.mode === 'override' ? <IconButton icon="reset" variant="secondary" label={t('slots.inheritance.reset')} disabled={disabled} onClick={() => setResetConfirmation(true)} /> : null}
    </IconButtonRow>
    {resetConfirmation ? <ConfirmationDialog title={t('slots.inheritance.reset')} detail={t('slots.inheritance.reset') + '?'} cancelLabel={t('slots.confirmCancel')} confirmLabel={t('slots.inheritance.reset')} confirmIcon="reset" confirmVariant="primary" onCancel={() => setResetConfirmation(false)} onConfirm={reset} /> : null}
  </>
}

export function AccessMeshDefaultsControls({ defaults, ...props }: {
  defaults: AccessMeshDefaultsReadModel & { inheritance?: PolicyInheritanceDisplay }
  canMutate: boolean
  externalBusy?: boolean
  execute: AccessMeshActionExecutor
  refresh: () => void
  notify: (message: string) => void
}) {
  return <div className="policy-controls"><NativePolicyForm {...props} policy={defaults.policy} legacyAdmissionEnabled={defaults.legacyAdmissionEnabled} revision={defaults.revision} inheritance={defaults.inheritance} policySupported={Boolean(defaults.managementContractVersion === 2 && defaults.inheritanceSupported && defaults.inheritance?.appliesTo === 'next_window')} /></div>
}

type SlotControlProps = {
  slot: NativeSlot
  canMutate: boolean
  externalBusy?: boolean
  execute: AccessMeshActionExecutor
  refresh: () => void
  notify: (message: string) => void
}

export function AccessMeshSlotControls({ slot, canMutate, externalBusy, execute, refresh, notify }: SlotControlProps) {
  const { t } = useI18n()
  const [windowBusy, setWindowBusy] = useState(false)
  return <div className="slot-subsection slot-policy-controls" aria-label={t('slots.managed.title')}>
    <NativePolicyForm key={slot.slotId} policySupported={Boolean(slot.managementContractVersion === 2 && slot.inheritanceSupported && slot.inheritance?.appliesTo === 'next_window')} policy={slot.policy} revision={slot.revision} inheritance={slot.inheritance} slotId={slot.slotId} canMutate={canMutate} externalBusy={externalBusy || windowBusy} execute={execute} refresh={refresh} notify={notify} />
    <CurrentWindowControls key={slot.slotId + ':' + (slot.sessionLifecycle.startedAt ?? '')} slot={slot} canMutate={canMutate} externalBusy={externalBusy} execute={execute} notify={notify} onBusyChange={setWindowBusy} />
  </div>
}

function CurrentWindowControls({ slot, canMutate, externalBusy, execute, notify, onBusyChange }: Omit<SlotControlProps, 'refresh'> & { onBusyChange: (busy: boolean) => void }) {
  const { t, locale } = useI18n()
  const [pendingDeadline, setPendingDeadline] = useState<AccessMeshMutationRequest | null>(null)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const cycle = slot.sessionLifecycle
  const startedAt = cycle.startedAt ?? ''
  const durationSeconds = startedAt && cycle.hardExpiresAt ? (Date.parse(cycle.hardExpiresAt) - Date.parse(startedAt)) / 1000 : null
  const [duration, setDuration] = useState(() => durationValue(durationSeconds ?? 0))
  const active = slot.managementContractVersion === 2 && ['active', 'warning', 'draining'].includes(cycle.state) && durationSeconds !== null
  const disabled = !canMutate || Boolean(externalBusy) || busy

  const confirmWindow = () => {
    if (disabled || !active) return
    const seconds = durationSecondsValue(duration)
    const deadline = seconds === null ? NaN : Date.parse(startedAt) + seconds * 1000
    if (seconds === null || seconds <= 0 || !Number.isFinite(deadline) || Math.abs(deadline) > 8640000000000000) { notify(t('slots.native.invalidPolicy')); return }
    if (deadline < Date.parse(cycle.hardExpiresAt ?? '')) { notify(t('slots.native.windowExtensionOnly')); return }
    setPendingDeadline({ action: 'deadline', slot_id: slot.slotId, expected_revision: slot.revision, deadline_at: new Date(deadline).toISOString(), idempotency_key: newSlotOperationKey() })
  }
  const applyWindow = async () => {
    const request = pendingDeadline
    setPendingDeadline(null)
    if (!request || disabled || busyRef.current) return
    busyRef.current = true; setBusy(true); onBusyChange(true)
    try {
      await execute(request)
    } catch { /* The action owner reports failures through the shared toast. */ }
    finally { busyRef.current = false; setBusy(false); onBusyChange(false) }
  }
  return <div className="slot-subsection"><h3>{t('slots.managed.window')}</h3>
      {slot.managementContractVersion !== 2 ? <p className="muted">{t('slots.native.sessionStateUnverified')}</p> : cycle.hardExpiresAt ? <dl className="slot-details-grid"><div><dt>{t('slots.managed.opened')}</dt><dd>{cycle.startedAt ?? t('common.unavailable')}</dd></div><div><dt>{t('slots.managed.deadline')}</dt><dd>{cycle.hardExpiresAt}</dd></div><div><dt>{t('slots.managed.remaining')}</dt><dd>{cycle.remainingSeconds === undefined ? t('common.unavailable') : formatDuration(cycle.remainingSeconds, locale)}</dd></div>
        {slot.windowPolicy ? <><div><dt>{t('slots.managed.duration')}</dt><dd>{formatDuration(slot.windowPolicy.durationSeconds, locale)}</dd></div><div><dt>{t('slots.managed.windowWarning')}</dt><dd>{formatDuration(slot.windowPolicy.warningSeconds, locale)}</dd></div><div><dt>{t('slots.managed.windowDraining')}</dt><dd>{formatDuration(slot.windowPolicy.drainingSeconds, locale)}</dd></div></> : null}
      </dl> : <p className="muted">{t('slots.managed.noWindow')}</p>}
      {active ? <><DurationField label={t('slots.managed.windowDuration')} value={duration} disabled={busy} onChange={setDuration} />
        <IconButtonRow className="server-actions policy-actions"><IconButton icon="save" variant="primary" label={t('slots.managed.saveWindow')} disabled={disabled} busy={busy} onClick={confirmWindow} /><IconButton icon="reset" variant="secondary" label={t('slots.managed.cancelWindow')} disabled={busy} onClick={() => setDuration(durationValue(durationSeconds ?? 0))} /></IconButtonRow>
      </> : null}
      {pendingDeadline ? <ConfirmationDialog title={t('slots.managed.windowConfirm')} subject={slot.publicName} detail={t('slots.managed.windowConfirm')} cancelLabel={t('slots.confirmCancel')} confirmLabel={t('slots.managed.applyWindow')} confirmIcon="save" confirmVariant="primary" onCancel={() => setPendingDeadline(null)} onConfirm={() => void applyWindow()} /> : null}
  </div>
}
