import { useEffect, useMemo, useState, type MouseEvent } from 'react'

import type { PersistentMutationResult } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { formatDuration } from '../i18n/duration'
import { useI18n } from '../i18n/useI18n'
import { UiButton } from './UiPrimitives'

export type ManagedSessionMutator = (
  instanceId: string,
  path: string,
  body: Record<string, unknown>,
) => Promise<PersistentMutationResult>

type ManagedWindowStatus = {
  workWindowId: string
  state: string
  openedAt: string
  elapsedSeconds: number
  remainingSeconds: number
  effectiveDurationSeconds: number
  hardExpiresAt: string
  warningBeforeExpirySeconds: number
  drainingBeforeExpirySeconds: number
  rearmAfterSeconds: number
  windowRevision: number
}

type ManagedSessionStatus = {
  logicalAgentId: string
  publicName: string
  authorityNodeId: string
  policy: {
    defaultDurationSeconds: number
    warningBeforeExpirySeconds: number
    drainingBeforeExpirySeconds: number
    rearmAfterSeconds: number
    revision: number
  }
  window?: ManagedWindowStatus
  session?: {
    workSessionId: string
    sessionEpoch: number
    role: string
    contractVersion: number
    state: string
    startedAt?: string
  }
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function integer(value: unknown, fallback = 0): number {
  return Number.isInteger(value) ? Number(value) : fallback
}

function decodeStatus(result: PersistentMutationResult): ManagedSessionStatus | undefined {
  if (!result.ok) return undefined
  const payload = result.payload
  const policy = record(payload.policy)
  if (!policy) return undefined
  const window = record(payload.window)
  const session = record(payload.session)
  const decodedWindow = window ? {
    workWindowId: text(window.work_window_id),
    state: text(window.state, text(window.lifecycle, 'unknown')),
    openedAt: text(window.opened_at),
    elapsedSeconds: integer(window.elapsed_seconds),
    remainingSeconds: integer(window.remaining_seconds),
    effectiveDurationSeconds: integer(window.effective_duration_seconds),
    hardExpiresAt: text(window.hard_expires_at),
    warningBeforeExpirySeconds: integer(window.warning_before_expiry_seconds),
    drainingBeforeExpirySeconds: integer(window.draining_before_expiry_seconds),
    rearmAfterSeconds: integer(window.rearm_after_seconds),
    windowRevision: integer(window.window_revision),
  } : undefined
  return {
    logicalAgentId: text(payload.logical_agent_id),
    publicName: text(payload.public_name, text(payload.logical_agent_id)),
    authorityNodeId: text(payload.authority_node_id),
    policy: {
      defaultDurationSeconds: integer(policy.default_duration_seconds),
      warningBeforeExpirySeconds: integer(policy.warning_before_expiry_seconds),
      drainingBeforeExpirySeconds: integer(policy.draining_before_expiry_seconds),
      rearmAfterSeconds: integer(policy.rearm_after_seconds),
      revision: integer(policy.revision),
    },
    window: decodedWindow,
    session: session ? {
      workSessionId: text(session.work_session_id),
      sessionEpoch: integer(session.session_epoch),
      role: text(session.role, 'unknown'),
      contractVersion: integer(session.contract_version),
      state: text(session.state, decodedWindow?.state ?? 'unknown'),
      startedAt: text(session.started_at) || undefined,
    } : undefined,
  }
}

function formNumber(button: HTMLButtonElement, name: string): number | undefined {
  const form = button.form
  if (!form) return undefined
  const raw = new FormData(form).get(name)
  if (typeof raw !== 'string' || !/^-?\d+$/.test(raw.trim())) return undefined
  const value = Number(raw)
  return Number.isSafeInteger(value) ? value : undefined
}

export function ManagedWorkSessions({
  instance,
  mutatePersistent,
}: {
  instance?: FleetInstanceView
  mutatePersistent?: ManagedSessionMutator
}) {
  const { locale } = useI18n()
  const [statuses, setStatuses] = useState<Record<string, ManagedSessionStatus>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const instanceId = instance?.profile.instanceId ?? ''
  const snapshotSlots = instance?.runtime.realtime?.snapshot?.persistent?.slots
  const slotIds = useMemo(
    () => (snapshotSlots ?? []).map((slot) => slot.logicalAgentId).sort(),
    [snapshotSlots],
  )

  const refresh = async (logicalAgentId: string) => {
    if (!instanceId || !mutatePersistent) return undefined
    const result = await mutatePersistent(instanceId, '/actions/persistent/managed/status', {
      logical_agent_id: logicalAgentId,
    })
    const status = decodeStatus(result)
    if (status) setStatuses((current) => ({ ...current, [logicalAgentId]: status }))
    return status
  }

  useEffect(() => {
    let cancelled = false
    if (!instanceId || !mutatePersistent || instance?.runtime.authStatus !== 'connected') {
      return () => { cancelled = true }
    }
    void Promise.all(slotIds.map(async (logicalAgentId) => {
      const result = await mutatePersistent(instanceId, '/actions/persistent/managed/status', {
        logical_agent_id: logicalAgentId,
      })
      return [logicalAgentId, decodeStatus(result)] as const
    })).then((values) => {
      if (cancelled) return
      setStatuses(Object.fromEntries(values.filter((item) => Boolean(item[1])) as Array<[string, ManagedSessionStatus]>))
    }).catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : 'managed_session_status_failed')
    })
    return () => { cancelled = true }
  }, [instance?.runtime.authStatus, instanceId, mutatePersistent, slotIds])

  const mutation = async (
    status: ManagedSessionStatus,
    path: string,
    body: Record<string, unknown>,
    label: string,
  ) => {
    if (!mutatePersistent || !instanceId) return
    setBusy(`${status.logicalAgentId}:${label}`)
    setError('')
    try {
      const result = await mutatePersistent(instanceId, path, {
        logical_agent_id: status.logicalAgentId,
        ...body,
      })
      if (!result.ok) {
        setError(result.code ?? result.error ?? 'managed_session_mutation_failed')
        return
      }
      await refresh(status.logicalAgentId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'managed_session_mutation_failed')
    } finally {
      setBusy('')
    }
  }

  const windowMutation = (
    status: ManagedSessionStatus,
    body: Record<string, unknown>,
    label: string,
  ) => {
    if (!status.window) return
    return mutation(status, '/actions/persistent/managed/window', {
      expected_revision: status.window.windowRevision,
      ...body,
    }, label)
  }

  const readButtonNumber = (event: MouseEvent<HTMLButtonElement>, name: string) => {
    const value = formNumber(event.currentTarget, name)
    if (value === undefined) setError('Enter an integer value.')
    return value
  }

  const items = Object.values(statuses).filter((status) => status.window || status.session)
  if (!instanceId || !mutatePersistent || items.length === 0) return null

  return (
    <section className="surface-section managed-work-sessions" aria-label="Managed work sessions">
      <div className="section-heading"><div><p className="eyebrow">Work windows</p><h3>Managed sessions</h3></div><span className="muted">{instance?.profile.displayName ?? instanceId}</span></div>
      {error ? <p role="alert" className="muted">{error}</p> : null}
      <div className="managed-work-session-list">
        {items.map((status) => {
          const window = status.window
          const session = status.session
          const disabled = !window || Boolean(busy)
          return (
            <form key={status.logicalAgentId} className="panel managed-work-session-card" onSubmit={(event) => event.preventDefault()}>
              <div className="section-heading"><div><p className="eyebrow">{status.authorityNodeId || instanceId}</p><h4>{status.publicName}</h4></div><span className="status server-status">{window?.state ?? session?.state ?? 'inactive'}</span></div>
              <dl className="slot-details-grid">
                <div><dt>Role / contract</dt><dd>{session ? `${session.role} · v${session.contractVersion}` : '—'}</dd></div>
                <div><dt>Session epoch</dt><dd>{session?.sessionEpoch ?? '—'}</dd></div>
                <div><dt>Elapsed</dt><dd>{window ? formatDuration(window.elapsedSeconds, locale) : '—'}</dd></div>
                <div><dt>Remaining</dt><dd>{window ? formatDuration(window.remainingSeconds, locale) : '—'}</dd></div>
                <div><dt>Effective duration</dt><dd>{window ? formatDuration(window.effectiveDurationSeconds, locale) : '—'}</dd></div>
                <div><dt>Hard expiry</dt><dd>{window?.hardExpiresAt ?? '—'}</dd></div>
              </dl>
              {window ? <>
                <div className="server-actions">
                  <UiButton type="button" variant="secondary" disabled={disabled} onClick={() => void windowMutation(status, { delta_seconds: 600 }, 'plus10')}>+10 min</UiButton>
                  <UiButton type="button" variant="secondary" disabled={disabled} onClick={() => void windowMutation(status, { delta_seconds: 1200 }, 'plus20')}>+20 min</UiButton>
                </div>
                <div className="policy-controls-grid">
                  <label className="ui-field"><span>Custom delta (seconds)</span><input name="delta" type="number" step="1" defaultValue="0" disabled={disabled} /></label>
                  <label className="ui-field"><span>Exact total duration (seconds)</span><input name="total" type="number" min="1" step="1" defaultValue={window.effectiveDurationSeconds} disabled={disabled} /></label>
                  <label className="ui-field"><span>Warning before expiry (seconds)</span><input name="warning" type="number" min="0" step="1" defaultValue={window.warningBeforeExpirySeconds} disabled={disabled} /></label>
                  <label className="ui-field"><span>Draining before expiry (seconds)</span><input name="draining" type="number" min="0" step="1" defaultValue={window.drainingBeforeExpirySeconds} disabled={disabled} /></label>
                </div>
                <div className="server-actions">
                  <UiButton type="button" variant="secondary" disabled={disabled} onClick={(event) => { const value = readButtonNumber(event, 'delta'); if (value !== undefined) void windowMutation(status, { delta_seconds: value }, 'delta') }}>Apply delta</UiButton>
                  <UiButton type="button" variant="secondary" disabled={disabled} onClick={(event) => { const value = readButtonNumber(event, 'total'); if (value !== undefined && value > 0) void windowMutation(status, { duration_seconds: value }, 'total') }}>Set total</UiButton>
                  <UiButton type="button" variant="secondary" disabled={disabled} onClick={(event) => { const warning = readButtonNumber(event, 'warning'); const draining = readButtonNumber(event, 'draining'); if (warning !== undefined && draining !== undefined) void windowMutation(status, { warning_before_expiry_seconds: warning, draining_before_expiry_seconds: draining }, 'thresholds') }}>Apply thresholds</UiButton>
                </div>
              </> : null}
              <details className="technical-details"><summary>Default slot policy</summary><div className="policy-controls-grid">
                <label className="ui-field"><span>Default duration (seconds)</span><input name="defaultDuration" type="number" min="1" step="1" defaultValue={status.policy.defaultDurationSeconds} /></label>
                <label className="ui-field"><span>Default warning before expiry</span><input name="defaultWarning" type="number" min="0" step="1" defaultValue={status.policy.warningBeforeExpirySeconds} /></label>
                <label className="ui-field"><span>Default draining before expiry</span><input name="defaultDraining" type="number" min="0" step="1" defaultValue={status.policy.drainingBeforeExpirySeconds} /></label>
                <label className="ui-field"><span>Rearm after (seconds)</span><input name="defaultRearm" type="number" min="0" step="1" defaultValue={status.policy.rearmAfterSeconds} /></label>
              </div><UiButton type="button" variant="secondary" disabled={Boolean(busy)} onClick={(event) => { const duration = readButtonNumber(event, 'defaultDuration'); const warning = readButtonNumber(event, 'defaultWarning'); const draining = readButtonNumber(event, 'defaultDraining'); const rearm = readButtonNumber(event, 'defaultRearm'); if ([duration, warning, draining, rearm].every((value) => value !== undefined)) void mutation(status, '/actions/persistent/managed/policy', { expected_revision: status.policy.revision, default_duration_seconds: duration, warning_before_expiry_seconds: warning, draining_before_expiry_seconds: draining, rearm_after_seconds: rearm }, 'policy') }}>Save default policy</UiButton></details>
              {session ? <div className="server-actions"><UiButton type="button" variant="destructive" disabled={Boolean(busy)} onClick={() => void mutation(status, '/actions/persistent/managed/sessions/end', {}, 'end')}>End work session</UiButton></div> : null}
            </form>
          )
        })}
      </div>
    </section>
  )
}
