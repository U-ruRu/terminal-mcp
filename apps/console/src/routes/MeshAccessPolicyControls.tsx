import { useRef, useState } from 'react'
import type { ManagedAccessPolicyReadModel } from '../api/models'
import { IconButton, IconButtonRow } from '../components/UiPrimitives'
import { useI18n } from '../i18n/useI18n'
import { DurationField } from '../slots/DurationField'
import { durationSecondsValue, durationValue, type DurationValue } from '../slots/duration'

/** Exact existing /actions/fleet/control/policy contract; it has one replicated authority. */
export type MeshAccessPolicyRequest = {
  duration_seconds: number
  warning_after_seconds: number
  alert_after_seconds: number
  rearm_after_seconds: number
  rearm_enabled: boolean
  legacy_admission_enabled: boolean
  expected_revision: number
}
export type MeshAccessPolicyOutcome = { confirmed: boolean; conflict?: boolean; savedPolicy?: ManagedAccessPolicyReadModel }
type Draft = { revision: number; duration: DurationValue; warning: DurationValue; alert: DurationValue; rearm: DurationValue; rearmEnabled: boolean; legacyAdmissionEnabled: boolean }
function draftFor(policy: ManagedAccessPolicyReadModel): Draft {
  return { revision: policy.revision, duration: durationValue(policy.durationSeconds),
    warning: durationValue(policy.warningAfterSeconds), alert: durationValue(policy.alertAfterSeconds),
    rearm: durationValue(policy.rearmAfterSeconds), rearmEnabled: policy.rearmEnabled, legacyAdmissionEnabled: policy.legacyAdmissionEnabled }
}


export function MeshAccessPolicyControls({ policy, canMutate, externalBusy, save, refresh, notify }: {
  policy: ManagedAccessPolicyReadModel
  canMutate: boolean
  externalBusy: boolean
  save: (request: MeshAccessPolicyRequest) => Promise<MeshAccessPolicyOutcome>
  refresh: () => void
  notify: (message: string) => void
}) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(() => draftFor(policy))
  const [busy, setBusy] = useState(false)
  const lock = useRef(false)
  const disabled = !canMutate || externalBusy || busy
  const submit = async () => {
    if (disabled || lock.current) return
    const duration = durationSecondsValue(draft.duration), warning = durationSecondsValue(draft.warning)
    const alert = durationSecondsValue(draft.alert), rearm = durationSecondsValue(draft.rearm)
    if (duration === null || warning === null || alert === null || rearm === null || !(0 < warning && warning < alert && alert < duration && rearm > 0)) {
      notify(t('slots.mesh.invalidPolicy')); return
    }
    lock.current = true; setBusy(true)
    try {
      const outcome = await save({ duration_seconds: duration, warning_after_seconds: warning, alert_after_seconds: alert,
        rearm_after_seconds: rearm, rearm_enabled: draft.rearmEnabled, legacy_admission_enabled: draft.legacyAdmissionEnabled, expected_revision: draft.revision })
      if (outcome.confirmed && outcome.savedPolicy) setDraft(draftFor(outcome.savedPolicy))
    } catch { /* The action owner reports failures through the shared toast. */ }
    finally { lock.current = false; setBusy(false) }
  }
  return <div className="policy-controls">
    <div className="section-heading"><h3>{t('slots.policyControls')}</h3><IconButton icon="retry" variant="quiet" label={t('slots.managed.refresh')} disabled={busy || externalBusy} onClick={refresh} /></div>
    <div className="policy-controls-grid">
      <DurationField label={t('slots.durationSeconds')} value={draft.duration} disabled={busy} onChange={(duration) => setDraft({ ...draft, duration })} />
      <DurationField label={t('slots.warningAfterSeconds')} value={draft.warning} disabled={busy} onChange={(warning) => setDraft({ ...draft, warning })} />
      <DurationField label={t('slots.alertAfterSeconds')} value={draft.alert} disabled={busy} onChange={(alert) => setDraft({ ...draft, alert })} />
      <DurationField label={t('slots.rearmAfterSeconds')} value={draft.rearm} disabled={busy} onChange={(rearm) => setDraft({ ...draft, rearm })} />
    </div>
    <label className="policy-toggle"><input type="checkbox" checked={draft.rearmEnabled} disabled={busy} onChange={(event) => setDraft({ ...draft, rearmEnabled: event.target.checked })} /><span>{t('slots.native.rearmEnabled')}</span></label>
    <label className="policy-toggle"><input type="checkbox" checked={draft.legacyAdmissionEnabled} disabled={busy} onChange={(event) => setDraft({ ...draft, legacyAdmissionEnabled: event.target.checked })} /><span>{t('slots.legacyAdmission')}</span></label>
    <IconButtonRow className="server-actions policy-actions">
      <IconButton icon="save" variant="primary" label={t('slots.savePolicy')} disabled={disabled} busy={busy} onClick={() => void submit()} />
      <IconButton icon="reset" variant="secondary" label={t('slots.managed.cancel')} disabled={busy} onClick={() => setDraft(draftFor(policy))} />
    </IconButtonRow>
  </div>
}
