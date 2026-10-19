import { useEffect, useState } from 'react'

import { ConsoleHttpError, type ConsoleClient } from '../api/client'
import type { NodeRuntimeOperation, NodeRuntimeStatus, NodeRuntimeTarget } from '../api/models'
import { ConfirmationDialog } from '../components/UiPrimitives'
import { useI18n } from '../i18n/useI18n'

type Api = Partial<Pick<ConsoleClient, 'nodeRuntime' | 'nodeRuntimeOperation' | 'restartNodeRuntime'>>
type Pending = { requestId: string; startedAt: number }
type Props = {
  instanceId: string
  deviceId: string
  enabled: boolean
  canRestart: boolean
  client: (instanceId: string) => Api | null | undefined
}

export function NodeRuntimePanel({ instanceId, deviceId, enabled, canRestart, client }: Props) {
  const { t } = useI18n()
  const storageKey = 'tmcp.runtime.' + instanceId + '.' + deviceId
  const [pending, setPending] = useState<Pending | null>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) ?? 'null') as Pending | null
      return value && /^[a-zA-Z0-9_-]{16,64}$/.test(value.requestId) && Number.isFinite(value.startedAt) ? value : null
    } catch { return null }
  })
  const [view, setView] = useState<NodeRuntimeStatus | null>(null)
  const [operation, setOperation] = useState<NodeRuntimeOperation | null>(null)
  const [confirmation, setConfirmation] = useState<NodeRuntimeTarget | null>(null)
  const [sending, setSending] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!enabled && !pending) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    async function observe() {
      const api = client(instanceId)
      if (!api?.nodeRuntime) return
      try {
        const current = await api.nodeRuntime()
        const receipt = pending && api.nodeRuntimeOperation
          ? await api.nodeRuntimeOperation(pending.requestId) : current.operation
        if (!alive) return
        setView(current)
        setOperation(receipt)
        setFailed(false)
        if (pending && receipt && !['pending', 'running'].includes(receipt.status)) {
          localStorage.removeItem(storageKey)
          setPending(null)
          return
        }
      } catch (cause) {
        if (!alive) return
        if (!pending && cause instanceof ConsoleHttpError && cause.status === 404) return
        if (pending && Date.now() - pending.startedAt > 180_000) {
          localStorage.removeItem(storageKey)
          setPending(null)
        }
        setFailed(true)
      }
      if (alive) timer = setTimeout(() => { void observe() }, pending ? 2000 : 30_000)
    }
    void observe()
    return () => { alive = false; clearTimeout(timer) }
  }, [client, enabled, instanceId, pending, storageKey])

  async function restart(target: NodeRuntimeTarget) {
    const api = client(instanceId)
    if (!api?.restartNodeRuntime || pending || sending || !canRestart) return
    const request = { requestId: crypto.randomUUID(), startedAt: Date.now() }
    // Record before sending. A disconnected reply is observed with the same ID.
    try { localStorage.setItem(storageKey, JSON.stringify(request)) } catch { setFailed(true); return }
    setSending(true)
    setFailed(false)
    setPending(request)
    try {
      const result = await api.restartNodeRuntime(target, request.requestId)
      setOperation(result)
    } catch (cause) {
      if (cause instanceof ConsoleHttpError && cause.status >= 400 && cause.status < 500) {
        localStorage.removeItem(storageKey)
        setPending(null)
      }
      setFailed(true)
    } finally { setSending(false) }
  }

  if (!view?.supported && !pending) return null
  const busy = sending || Boolean(pending)
  const label = (target: NodeRuntimeTarget) => t(target === 'terminal' ? 'runtime.terminal' : 'runtime.tunnel')
  return <section className="ui-field" aria-label={t('runtime.title')}>
    <strong>{t('runtime.title')}</strong>
    <div className="connection-fleet-status">
      {(['terminal', 'tunnel'] as const).filter((target) => view?.services[target]).map((target) => (
        <span key={target}>{label(target)}: {t(`runtime.${view!.services[target]!.state}`)}</span>
      ))}
    </div>
    <div className="membership-target-options">
      {view?.restart_targets.map((target) => <button key={target} type="button" className="chip"
        disabled={!canRestart || busy} onClick={() => setConfirmation(target)}>
        {t('runtime.restart')} · {label(target)}
      </button>)}
    </div>
    <p className="muted" role={failed || operation?.status === 'failed' || operation?.status === 'unknown' ? 'alert' : 'status'} aria-live="polite">
      {failed ? t(pending ? 'runtime.reconnecting' : 'runtime.unconfirmed')
        : operation ? t(`runtime.${operation.status}`) : '\u00a0'}
    </p>
    {confirmation ? <ConfirmationDialog title={t('runtime.restart')} subject={label(confirmation)}
      detail={t(confirmation === 'terminal' ? 'runtime.terminalConfirm' : 'runtime.tunnelConfirm')}
      cancelLabel={t('connections.handoff.cancel')} confirmLabel={t('runtime.restart')} confirmIcon="retry"
      onCancel={() => setConfirmation(null)} onConfirm={() => {
        const target = confirmation
        setConfirmation(null)
        void restart(target)
      }} /> : null}
  </section>
}
