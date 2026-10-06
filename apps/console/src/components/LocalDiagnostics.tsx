import { useEffect, useState } from 'react'

import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import type { FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState, IconButton, IconButtonRow } from './UiPrimitives'

function connectionLabel(value: string, t: ReturnType<typeof useI18n>['t']): string {
  if (value === 'live') return t('status.live')
  if (value === 'offline') return t('status.offline')
  if (value === 'stale') return t('status.stale')
  if (value === 'reconnecting') return t('status.reconnecting')
  if (value === 'connecting') return t('status.connecting')
  return t('common.unknown')
}

function diagnosticError(code: string | undefined, t: ReturnType<typeof useI18n>['t']): string {
  if (!code) return t('diagnostics.connectionError')
  if (code === 'auth_unpaired') return t('diagnostics.serverUnpaired')
  if (code === 'direct_authority_auth_revoked' || code === 'auth_revoked') return t('diagnostics.authorizationRevoked')
  return t('diagnostics.connectionError')
}

export function LocalDiagnostics({ server, diagnostics }: { server: FleetServerReadModel; diagnostics?: BrowserDiagnosticJournal }) {
  const { t, dateTime, number } = useI18n()
  const [, setRevision] = useState(0)
  const [copied, setCopied] = useState(false)
  useEffect(() => diagnostics?.subscribe(() => setRevision((value) => value + 1)), [diagnostics])
  if (!diagnostics) return null
  const entries = diagnostics.list(server.instanceId)

  async function copyDiagnostics() {
    const text = diagnostics!.exportText(server.instanceId)
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      textarea.remove()
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }

  return (
    <details className="diagnostics-panel server-overview-diagnostics">
      <summary>{t('diagnostics.title')} · {number(entries.length)}</summary>
      <p className="muted">{t('diagnostics.description')}</p>
      {server.lastError ? <p className="connection-error" role="status">{diagnosticError(server.lastError, t)}</p> : null}
      {server.lastError || server.staleReason ? (
        <details className="inline-technical-details">
          <summary>{t('diagnostics.technicalDetails')}</summary>
          {server.lastError ? <code>{server.lastError}</code> : null}
          {server.staleReason ? <code>{server.staleReason}</code> : null}
        </details>
      ) : null}
      <IconButtonRow className="diagnostics-actions">
        <IconButton icon="delete" variant="destructive" label={t('diagnostics.clear')} onClick={() => diagnostics.clear()} disabled={entries.length === 0} />
        <IconButton icon="copy" variant="secondary" label={copied ? t('diagnostics.copied') : t('diagnostics.copy')} onClick={() => void copyDiagnostics()} disabled={entries.length === 0} />
      </IconButtonRow>
      {entries.length === 0 ? <FeedbackState variant="empty" title={t('diagnostics.empty')} /> : (
        <div className="diagnostics-log" aria-label={t('diagnostics.title')}>
          {entries.map((event) => (
            <div className="diagnostics-entry" key={event.seq}>
              <div className="diagnostics-entry-heading">
                <time dateTime={event.at}>{dateTime(event.at)}</time>
                <strong>{event.code ? diagnosticError(event.code, t) : event.status ? connectionLabel(event.status, t) : t('diagnostics.connectionError')}</strong>
              </div>
              <details className="inline-technical-details">
                <summary>{t('diagnostics.technicalDetails')}</summary>
                <code>#{event.seq} {event.type}</code>
                <div className="muted">{[event.server, event.status, event.authStatus ? `auth=${event.authStatus}` : '', event.code ? `code=${event.code}` : ''].filter(Boolean).join(' · ')}</div>
                {event.detail ? <div className="diagnostics-detail">{event.detail}</div> : null}
              </details>
            </div>
          ))}
        </div>
      )}
    </details>
  )
}
