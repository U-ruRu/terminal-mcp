import { App as CapacitorApp } from '@capacitor/app'
import { Capacitor } from '@capacitor/core'
import { useCallback, useEffect, useRef, useState } from 'react'
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
  const returnFocus = useRef<HTMLElement | null>(null)
  const receive = useCallback((url: string, trustedOrigin: string) => {
    try {
      const { origin, name } = inspectPairingHandoff(url, trustedOrigin)
      if (!returnFocus.current) returnFocus.current = document.activeElement as HTMLElement | null
      setPending({ url, origin, name }); setHandoffError(null)
    } catch {
      if (!returnFocus.current) returnFocus.current = document.activeElement as HTMLElement | null
      setPending(null); setHandoffError(t('connections.handoff.invalid'))
    }
  }, [t])
  useEffect(() => {
    if (location.pathname === '/connect' && location.hash) {
      const href = window.location.href
      const origin = window.location.origin
      void Promise.resolve().then(() => {
        receive(href, origin)
        // Cold web handoffs have no prior reading context; scrub the one-time secret from the URL.
        navigate('/connections', { replace: true })
      })
    }
  }, [location.hash, location.pathname, navigate, receive])
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
    setSubmitting(true)
    try {
      await pair(link, name)
      setPending(null)
      const target = returnFocus.current
      returnFocus.current = null
      window.setTimeout(() => target?.focus(), 0)
    } catch { /* runtime owns safe error */ } finally { setSubmitting(false) }
  }
  const close = () => {
    setPending(null); setHandoffError(null)
    const target = returnFocus.current
    returnFocus.current = null
    window.setTimeout(() => target?.focus(), 0)
  }
  if (!pending && !handoffError) return null
  return <aside className="panel pairing-handoff" role="dialog" aria-modal="true" aria-labelledby="pairing-handoff-title">
    <h2 id="pairing-handoff-title">{t('connections.pairTerminal')}</h2>
    {pending ? <>
      <p>{t('connections.handoff.connectPrefix')} <strong>{pending.origin}</strong>?</p>
      <p className="muted">{t('connections.handoff.secretMemory')}</p>
      <div className="connection-actions">
        <button type="button" disabled={submitting} onClick={() => void confirm()}>{submitting ? t('connections.pairing') : t('connections.handoff.pairServer')}</button>
        <button type="button" className="secondary-action" onClick={close}>{t('connections.handoff.cancel')}</button>
      </div>
    </> : <>
      <p className="connection-error" role="alert">{handoffError}</p>
      <button type="button" className="secondary-action" onClick={close}>{t('connections.handoff.dismiss')}</button>
    </>}
  </aside>
}
