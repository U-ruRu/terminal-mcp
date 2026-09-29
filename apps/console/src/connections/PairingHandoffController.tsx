import { App as CapacitorApp } from '@capacitor/app'
import { Capacitor } from '@capacitor/core'
import { useCallback, useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { attachAppUrlSource, inspectPairingHandoff, PRODUCTION_CONSOLE_ORIGIN } from './handoff'
import { useI18n } from '../i18n/useI18n'
import { useConnectionRuntime } from './runtime'

type Pending = { url: string; origin: string; name: string }

export function PairingHandoffController() {
  const { pair } = useConnectionRuntime()
  const { t } = useI18n()
  const location = useLocation()
  const navigate = useNavigate()
  const [pending, setPending] = useState<Pending | null>(null)
  const [handoffError, setHandoffError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const receive = useCallback((url: string, trustedOrigin: string) => {
    try {
      const { origin, name } = inspectPairingHandoff(url, trustedOrigin)
      setPending({ url, origin, name }); setHandoffError(null)
    } catch {
      setPending(null); setHandoffError(t('connections.handoff.invalid'))
    }
    navigate('/connections', { replace: true })
  }, [navigate, t])
  useEffect(() => {
    if (location.pathname === '/connect' && location.hash) {
      const href = window.location.href
      const origin = window.location.origin
      void Promise.resolve().then(() => receive(href, origin))
    }
  }, [location.hash, location.pathname, receive])
  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return
    let disposed = false
    let detach: (() => Promise<void>) | undefined
    void attachAppUrlSource(CapacitorApp, (url) => receive(url, PRODUCTION_CONSOLE_ORIGIN)).then((cleanup) => {
      if (disposed) void cleanup(); else detach = cleanup
    })
    return () => { disposed = true; if (detach) void detach() }
  }, [receive])
  async function confirm() {
    if (!pending) return
    const link = pending.url
    const name = pending.name
    setPending(null); setSubmitting(true)
    try { await pair(link, name) } catch { /* runtime owns safe error */ } finally { setSubmitting(false) }
  }
  if (!pending && !handoffError) return null
  return <aside className="panel pairing-handoff" role="dialog" aria-labelledby="pairing-handoff-title">
    <h2 id="pairing-handoff-title">{t('connections.pairTerminal')}</h2>
    {pending ? <>
      <p>{t('connections.handoff.connectPrefix')} <strong>{pending.origin}</strong>?</p>
      <p className="muted">{t('connections.handoff.secretMemory')}</p>
      <div className="connection-actions">
        <button type="button" disabled={submitting} onClick={() => void confirm()}>{submitting ? t('connections.pairing') : t('connections.handoff.pairServer')}</button>
        <button type="button" className="secondary-action" onClick={() => setPending(null)}>{t('connections.handoff.cancel')}</button>
      </div>
    </> : <>
      <p className="connection-error" role="alert">{handoffError}</p>
      <button type="button" className="secondary-action" onClick={() => setHandoffError(null)}>{t('connections.handoff.dismiss')}</button>
    </>}
  </aside>
}
