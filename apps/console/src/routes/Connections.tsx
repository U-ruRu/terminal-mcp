import { type FormEvent, useState } from 'react'

import { useConnectionRuntime } from '../connections/runtime'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'

function statusLabel(status: string | undefined, t: (key: MessageKey) => string): string {
  switch (status) {
    case 'connected':
      return t('connections.status.connected')
    case 'restoring':
      return t('connections.status.restoring')
    case 'revoked':
      return t('connections.status.revoked')
    case 'expired':
      return t('connections.status.expired')
    case 'error':
      return t('connections.status.reconnectNeeded')
    default:
      return t('connections.status.stored')
  }
}

export function Connections() {
  const { profiles, states, error, pair, retry, disconnect } = useConnectionRuntime()
  const { t, number } = useI18n()
  const [pairingLink, setPairingLink] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    try {
      await pair(pairingLink, displayName || undefined)
      setPairingLink('')
      setDisplayName('')
    } catch {
      // Runtime exposes a safe user-visible error without credential material.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="stack" aria-labelledby="connections-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t('connections.fleetAccess')}</p>
          <h2 id="connections-title">{t('connections.title')}</h2>
          <p className="muted">{t('connections.restoreHint')}</p>
        </div>
        <span className="environment-badge">{number(profiles.length)} {t('connections.saved')}</span>
      </div>

      <form className="panel connection-form" onSubmit={onSubmit}>
        <div>
          <p className="eyebrow">{t('connections.addServer')}</p>
          <h3>{t('connections.pairTerminal')}</h3>
        </div>
        <label>
          <span>{t('connections.pairingLink')}</span>
          <input
            required
            type="url"
            placeholder={t('connections.pairingPlaceholder')}
            value={pairingLink}
            onChange={(event) => setPairingLink(event.target.value)}
          />
        </label>
        <label>
          <span>{t('connections.displayName')}</span>
          <input
            type="text"
            maxLength={120}
            placeholder={t('connections.optional')}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </label>
        <button type="submit" disabled={submitting}>
          {submitting ? t('connections.pairing') : t('connections.addServerAction')}
        </button>
        {error ? <p className="connection-error" role="alert">{error}</p> : null}
      </form>

      <div className="connection-list">
        {profiles.length === 0 ? (
          <article className="panel">
            <h3>{t('connections.noPairedServers')}</h3>
            <p className="muted">{t('connections.usePairingLink')}</p>
          </article>
        ) : (
          profiles.map((profile) => {
            const state = states[profile.instanceId]
            return (
              <article className="panel connection-card" key={profile.instanceId}>
                <div className="connection-card-heading">
                  <div>
                    <h3>{profile.displayName}</h3>
                    <p className="muted">{profile.origin}</p>
                  </div>
                  <span className={'status connection-status-' + (state?.status ?? 'stored')}>
                    {statusLabel(state?.status, t)}
                  </span>
                </div>
                <p className="muted">{t('connections.device')}: {profile.metadata.deviceLabel}</p>
                {state?.status === 'error' ? (
                  <p className="connection-error">
                    {state.message}{state.retryable ? ' — ' + t('connections.retryAvailable') : ''}
                  </p>
                ) : null}
                <div className="connection-actions">
                  {state?.status === 'error' && state.retryable ? (
                    <button type="button" onClick={() => void retry(profile.instanceId)}>{t('connections.retry')}</button>
                  ) : null}
                  <button
                    type="button"
                    className="secondary-action"
                    onClick={() => disconnect(profile.instanceId)}
                  >
                    {t('connections.remove')}
                  </button>
                </div>
              </article>
            )
          })
        )}
      </div>
    </section>
  )
}
