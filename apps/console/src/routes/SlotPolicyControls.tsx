import { useEffect, useRef, useState } from 'react'

import { ConsoleHttpError } from '../api/client'
import type { ManagedSlotStatusReadModel, PersistentMutationResult } from '../api/models'
import { ConfirmationDialog, FeedbackState, IconButton, IconButtonRow } from '../components/UiPrimitives'
import type { MessageKey } from '../i18n/catalogs'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { durationSecondsValue, durationValue, type DurationUnit, type DurationValue } from '../slots/duration'

export type ManagedSlotStatusLoader = (instanceId: string, logicalAgentId: string) => Promise<ManagedSlotStatusReadModel>
type Mutator = (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>
type TimingValues = { duration: DurationValue; warning: DurationValue; draining: DurationValue; rearm: DurationValue }
type PolicyDraft = TimingValues & { revision: number }
type WindowDraft = Omit<TimingValues, 'rearm'> & { revision: number; windowId: string }
type ReadState = { key: string; status?: ManagedSlotStatusReadModel; error?: MessageKey; code?: string }

function publicCode(error: unknown): string {
  const value = error instanceof ConsoleHttpError ? error.code : error instanceof Error ? error.message : ''
  // Never echo arbitrary network messages or response bodies: they can contain credentials.
  return /^[a-z][a-z0-9_]{0,79}$/.test(value) ? value : 'request_failed'
}

function errorLabel(error: unknown): MessageKey {
  const code = publicCode(error)
  if (error instanceof ConsoleHttpError && [404, 405, 501].includes(error.status)) return 'slots.managed.unsupported'
  if (error instanceof ConsoleHttpError && [401, 403].includes(error.status)) return 'slots.managed.forbidden'
  if (/permission|forbidden|insufficient_scope|unauthorized/.test(code)) return 'slots.managed.forbidden'
  if (/not_supported|unsupported|not_implemented/.test(code)) return 'slots.managed.unsupported'
  return 'slots.managed.loadFailed'
}

function policyValues(status: ManagedSlotStatusReadModel): PolicyDraft {
  return {
    revision: status.policy.revision,
    duration: durationValue(status.policy.defaultDurationSeconds),
    warning: durationValue(status.policy.warningBeforeExpirySeconds),
    draining: durationValue(status.policy.drainingBeforeExpirySeconds),
    rearm: durationValue(status.policy.rearmAfterSeconds),
  }
}

function windowValues(status: ManagedSlotStatusReadModel): WindowDraft | null {
  const window = status.window
  return window ? {
    revision: window.windowRevision,
    windowId: window.workWindowId,
    duration: durationValue(window.effectiveDurationSeconds),
    warning: durationValue(window.warningBeforeExpirySeconds),
    draining: durationValue(window.drainingBeforeExpirySeconds),
  } : null
}

function DurationField({ label, value, disabled, onChange }: {
  label: string
  value: DurationValue
  disabled: boolean
  onChange: (value: DurationValue) => void
}) {
  const { t } = useI18n()
  return <label className="ui-field"><span>{label}</span><span className="duration-control">
    <input aria-label={label} inputMode="numeric" type="number" min="0" step="1" value={value.amount} disabled={disabled} onChange={(event) => onChange({ ...value, amount: event.target.value })} />
    <select aria-label={label + ' · ' + t('slots.unit')} value={value.unit} disabled={disabled} onChange={(event) => onChange({ ...value, unit: event.target.value as DurationUnit })}>
      <option value="seconds">{t('slots.unit.seconds')}</option><option value="minutes">{t('slots.unit.minutes')}</option><option value="hours">{t('slots.unit.hours')}</option>
    </select>
  </span></label>
}

/** Slot defaults and the already-open work window have separate revisions and lifetimes. */
export function SlotPolicyControls({ instanceId, logicalAgentId, connected, loadStatus, mutate }: {
  instanceId: string
  logicalAgentId: string
  connected: boolean
  loadStatus?: ManagedSlotStatusLoader
  mutate?: Mutator
}) {
  const { t, locale } = useI18n()
  const key = instanceId + ':' + logicalAgentId
  const [read, setRead] = useState<ReadState>({ key: '' })
  const [reload, setReload] = useState(0)
  const [policyDraft, setPolicyDraft] = useState<PolicyDraft | null>(null)
  const [windowDraft, setWindowDraft] = useState<WindowDraft | null>(null)
  const [pendingWindow, setPendingWindow] = useState<Record<string, unknown> | null>(null)
  const [busy, setBusy] = useState('')
  const busyRef = useRef(false)
  const [notice, setNotice] = useState<MessageKey | null>(null)
  const [mutationCode, setMutationCode] = useState('')
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const generation = useRef(0)

  useEffect(() => {
    if (!loadStatus || !connected) return
    const requestGeneration = ++generation.current
    let cancelled = false
    void loadStatus(instanceId, logicalAgentId).then((status) => {
      if (cancelled || requestGeneration !== generation.current) return
      setRead({ key, status })
      setNeedsRefresh(false)
    }).catch((error: unknown) => {
      if (cancelled || requestGeneration !== generation.current) return
      setRead((current) => ({ key, status: current.key === key ? current.status : undefined, error: errorLabel(error), code: publicCode(error) }))
    })
    return () => { cancelled = true }
  }, [connected, instanceId, key, loadStatus, logicalAgentId, reload])

  const status = read.key === key ? read.status : undefined
  const loadError = read.key === key ? read.error : undefined
  const draft = policyDraft ?? (status ? policyValues(status) : undefined)
  const currentWindowDraft = windowDraft ?? (status ? windowValues(status) : null)
  const policyConflict = Boolean(policyDraft && status && policyDraft.revision !== status.policy.revision)
  const windowConflict = Boolean(windowDraft && (!status?.window || windowDraft.windowId !== status.window.workWindowId || windowDraft.revision !== status.window.windowRevision))
  const writable = Boolean(mutate && loadStatus && connected && status && !needsRefresh && !loadError)

  const refresh = async (): Promise<ManagedSlotStatusReadModel | undefined> => {
    if (!loadStatus) return undefined
    ++generation.current
    try {
      const value = await loadStatus(instanceId, logicalAgentId)
      setRead({ key, status: value })
      setNeedsRefresh(false)
      return value
    } catch (error) {
      setRead((current) => ({ key, status: current.key === key ? current.status : undefined, error: errorLabel(error), code: publicCode(error) }))
      setNeedsRefresh(true)
      return undefined
    }
  }

  const perform = async (action: 'policy' | 'window', body: Record<string, unknown>) => {
    if (!writable || !mutate || busyRef.current) return
    // React state is not a synchronous exclusion lock for two events in one render.
    busyRef.current = true
    setBusy(action)
    setNotice(null)
    setMutationCode('')
    let accepted = false
    try {
      const result = await mutate(instanceId, `/actions/persistent/managed/${action}`, { logical_agent_id: logicalAgentId, ...body })
      if (!result.ok) {
        const code = result.code ?? result.error ?? 'request_failed'
        setMutationCode(/^[a-z][a-z0-9_]{0,79}$/.test(code) ? code : 'request_failed')
        setNotice(/revision|conflict/.test(code) ? 'slots.managed.conflict' : 'slots.managed.saveFailed')
      } else accepted = true
    } catch (error) {
      setMutationCode(publicCode(error))
      setNeedsRefresh(true)
      setNotice('slots.managed.uncertain')
    }
    // An HTTP success alone is not the authoritative state shown by the form.
    const confirmed = await refresh()
    if (accepted && confirmed) {
      if (action === 'policy') setPolicyDraft(null)
      else setWindowDraft(null)
      setNotice('slots.managed.confirmed')
    } else if (accepted) setNotice('slots.managed.confirmationPending')
    busyRef.current = false
    setBusy('')
  }

  const savePolicy = () => {
    if (!draft || policyConflict) return
    const duration = durationSecondsValue(draft.duration)
    const warning = durationSecondsValue(draft.warning)
    const draining = durationSecondsValue(draft.draining)
    const rearm = durationSecondsValue(draft.rearm)
    // WorkWindow domain permits thresholds >= duration and zero cooldown; only order is constrained.
    if (duration === null || duration <= 0 || warning === null || draining === null || rearm === null || draining > warning) {
      setNotice('slots.managed.invalid'); return
    }
    void perform('policy', { expected_revision: draft.revision, default_duration_seconds: duration, warning_before_expiry_seconds: warning, draining_before_expiry_seconds: draining, rearm_after_seconds: rearm })
  }

  const saveWindow = () => {
    if (!currentWindowDraft || windowConflict) return
    const duration = durationSecondsValue(currentWindowDraft.duration)
    const warning = durationSecondsValue(currentWindowDraft.warning)
    const draining = durationSecondsValue(currentWindowDraft.draining)
    if (duration === null || duration <= 0 || warning === null || draining === null || draining > warning) {
      setNotice('slots.managed.invalid'); return
    }
    // Absolute duration is measured from opened_at, not an addition to time remaining.
    setPendingWindow({ expected_revision: currentWindowDraft.revision, duration_seconds: duration, warning_before_expiry_seconds: warning, draining_before_expiry_seconds: draining })
  }

  const disabled = !writable || Boolean(busy)
  return <div className="slot-subsection slot-policy-controls" aria-label={t('slots.managed.title')}>
    <div className="section-heading"><h3>{t('slots.managed.title')}</h3><IconButton icon="retry" variant="quiet" label={t('slots.managed.refresh')} disabled={!connected || !loadStatus || Boolean(busy)} onClick={() => setReload((value) => value + 1)} /></div>
    {!loadStatus ? <FeedbackState variant="partial" title={t('slots.managed.unsupported')} /> : !connected ? <FeedbackState variant="partial" title={t('slots.liveRequired')} /> : !status && !loadError ? <p role="status" className="muted">{t('slots.managed.loading')}</p> : null}
    {pendingWindow ? <ConfirmationDialog title={t('slots.managed.windowConfirm')} subject={status?.publicName ?? logicalAgentId} detail={t('slots.managed.windowHint')} cancelLabel={t('slots.confirmCancel')} confirmLabel={t('slots.managed.applyWindow')} confirmIcon="save" confirmVariant="primary" onCancel={() => setPendingWindow(null)} onConfirm={() => { const body = pendingWindow; setPendingWindow(null); void perform('window', body) }} /> : null}
    {loadError ? <FeedbackState variant="error" title={t(loadError)} detail={read.code} /> : null}
    {notice ? <p role="status" className="attention-strip">{t(notice)}{mutationCode ? ` · ${mutationCode}` : ''}</p> : null}
    {status && draft ? <>
      <p className="muted">{t('slots.managed.nextWindow')}</p>
      <div className="policy-controls-grid">
        <DurationField label={t('slots.managed.duration')} value={draft.duration} disabled={disabled} onChange={(duration) => setPolicyDraft({ ...draft, duration })} />
        <DurationField label={t('slots.managed.warning')} value={draft.warning} disabled={disabled} onChange={(warning) => setPolicyDraft({ ...draft, warning })} />
        <DurationField label={t('slots.managed.draining')} value={draft.draining} disabled={disabled} onChange={(draining) => setPolicyDraft({ ...draft, draining })} />
        <DurationField label={t('slots.managed.rearm')} value={draft.rearm} disabled={disabled} onChange={(rearm) => setPolicyDraft({ ...draft, rearm })} />
      </div>
      {policyConflict ? <p role="alert" className="attention-strip">{t('slots.managed.conflict')}</p> : null}
      <IconButtonRow className="server-actions policy-actions">
        <IconButton icon="save" variant="primary" label={t('slots.managed.savePolicy')} busy={busy === 'policy'} disabled={disabled || policyConflict} onClick={savePolicy} />
        <IconButton icon="reset" variant="secondary" label={t('slots.managed.cancel')} disabled={Boolean(busy) || !policyDraft} onClick={() => { setPolicyDraft(null); setNotice(null) }} />
      </IconButtonRow>
      <div className="slot-subsection">
        <h3>{t('slots.managed.window')}</h3>
        {status.window && currentWindowDraft ? <>
          <p className="muted">{t('slots.managed.windowHint')}</p>
          <dl className="slot-details-grid">
            <div><dt>{t('slots.managed.opened')}</dt><dd>{status.window.openedAt}</dd></div>
            <div><dt>{t('slots.managed.deadline')}</dt><dd>{status.window.hardExpiresAt}</dd></div>
            <div><dt>{t('slots.managed.remaining')}</dt><dd>{formatDuration(status.window.remainingSeconds, locale)}</dd></div>
            <div><dt>{t('slots.managed.phase')}</dt><dd>{status.window.state}</dd></div>
          </dl>
          {status.window.lifecycle === 'open' ? <>
            <div className="policy-controls-grid">
              <DurationField label={t('slots.managed.windowDuration')} value={currentWindowDraft.duration} disabled={disabled} onChange={(duration) => setWindowDraft({ ...currentWindowDraft, duration })} />
              <DurationField label={t('slots.managed.windowWarning')} value={currentWindowDraft.warning} disabled={disabled} onChange={(warning) => setWindowDraft({ ...currentWindowDraft, warning })} />
              <DurationField label={t('slots.managed.windowDraining')} value={currentWindowDraft.draining} disabled={disabled} onChange={(draining) => setWindowDraft({ ...currentWindowDraft, draining })} />
            </div>
            {windowConflict ? <p role="alert" className="attention-strip">{t('slots.managed.conflict')}</p> : null}
            <IconButtonRow className="server-actions policy-actions">
              <IconButton icon="save" variant="primary" label={t('slots.managed.saveWindow')} busy={busy === 'window'} disabled={disabled || windowConflict} onClick={saveWindow} />
              <IconButton icon="reset" variant="secondary" label={t('slots.managed.cancelWindow')} disabled={Boolean(busy) || !windowDraft} onClick={() => { setWindowDraft(null); setNotice(null) }} />
            </IconButtonRow>
          </> : <p className="muted">{t('slots.managed.windowClosed')}</p>}
        </> : <p className="muted">{t('slots.managed.noWindow')}</p>}
      </div>
    </> : null}
  </div>
}
