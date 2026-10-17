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
  expected_revision: number
}
export type MeshAccessPolicyOutcome = { confirmed: boolean; conflict?: boolean }
type Draft = { revision: number; base: string; duration: DurationValue; warning: DurationValue; alert: DurationValue; rearm: DurationValue }
function draftFor(policy: ManagedAccessPolicyReadModel): Draft {
  return { revision: policy.revision, base: JSON.stringify(policy), duration: durationValue(policy.durationSeconds),
    warning: durationValue(policy.warningAfterSeconds), alert: durationValue(policy.alertAfterSeconds),
    rearm: durationValue(policy.rearmAfterSeconds) }
}


export function MeshAccessPolicyControls({ policy, canMutate, externalBusy, authorityName, save, refresh }: {
  policy: ManagedAccessPolicyReadModel
  canMutate: boolean
  externalBusy: boolean
  authorityName: string
  save: (request: MeshAccessPolicyRequest) => Promise<MeshAccessPolicyOutcome>
  refresh: () => void
}) {
  const { t } = useI18n()
  const [edited, setEdited] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const lock = useRef(false)
  const [notice, setNotice] = useState('')
  const draft = edited ?? draftFor(policy)
  const conflict = Boolean(edited && edited.base !== JSON.stringify(policy))
  const disabled = !canMutate || externalBusy || busy
  const submit = async () => {
    if (disabled || conflict || lock.current) return
    const duration = durationSecondsValue(draft.duration), warning = durationSecondsValue(draft.warning)
    const alert = durationSecondsValue(draft.alert), rearm = durationSecondsValue(draft.rearm)
    if (duration === null || warning === null || alert === null || rearm === null || !(0 < warning && warning < alert && alert < duration && rearm > 0)) {
      setNotice(t('slots.mesh.invalidPolicy')); return
    }
    lock.current = true; setBusy(true); setNotice('')
    try {
      const outcome = await save({ duration_seconds: duration, warning_after_seconds: warning, alert_after_seconds: alert,
        rearm_after_seconds: rearm, expected_revision: draft.revision })
      if (outcome.confirmed) { setEdited(null); setNotice(t('slots.managed.confirmed')) }
      else setNotice(t(outcome.conflict ? 'slots.managed.conflict' : 'slots.managed.confirmationPending'))
    } catch { setNotice(t('slots.managed.uncertain')) }
    finally { lock.current = false; setBusy(false) }
  }
  return <div className="policy-controls">
    <div className="section-heading"><h3>{t('slots.policyControls')}</h3><IconButton icon="retry" variant="quiet" label={t('slots.managed.refresh')} disabled={busy || externalBusy} onClick={refresh} /></div>
    <p className="muted">{t('slots.inheritance.mesh')} · {t('connections.controlNode')}: {authorityName} · {t('slots.revision')} {policy.revision}</p>
    <p className="muted">{t('slots.managed.nextWindow')}</p><p className="muted">{t('slots.mesh.elapsedHint')}</p>
    {!canMutate ? <p role="status" className="attention-strip">{t('slots.mesh.freshAuthorityRequired')}</p> : null}
    <div className="policy-controls-grid">
      <DurationField label={t('slots.durationSeconds')} value={draft.duration} disabled={disabled} onChange={(duration) => setEdited({ ...draft, duration })} />
      <DurationField label={t('slots.warningAfterSeconds')} value={draft.warning} disabled={disabled} onChange={(warning) => setEdited({ ...draft, warning })} />
      <DurationField label={t('slots.alertAfterSeconds')} value={draft.alert} disabled={disabled} onChange={(alert) => setEdited({ ...draft, alert })} />
      <DurationField label={t('slots.rearmAfterSeconds')} value={draft.rearm} disabled={disabled} onChange={(rearm) => setEdited({ ...draft, rearm })} />
    </div>
    {conflict ? <p role="alert" className="attention-strip">{t('slots.managed.conflict')}</p> : null}
    {notice ? <p role="status" className="attention-strip">{notice}</p> : null}
    <IconButtonRow className="server-actions policy-actions">
      <IconButton icon="save" variant="primary" label={t('slots.savePolicy')} disabled={disabled || conflict} busy={busy} onClick={() => void submit()} />
      <IconButton icon="reset" variant="secondary" label={t('slots.managed.cancel')} disabled={busy || !edited} onClick={() => { setEdited(null); setNotice('') }} />
    </IconButtonRow>
  </div>
}
