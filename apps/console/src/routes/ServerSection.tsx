import { useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import type { FleetReadModel } from '../fleet/readModel'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'

type ServerSectionKind = 'agents' | 'context' | 'health'

const titles: Record<ServerSectionKind, MessageKey> = {
  agents: 'nav.agents',
  context: 'nav.context',
  health: 'section.healthDiagnostics',
}

export function ServerSection({
  model,
  section,
  diagnostics,
}: {
  model: FleetReadModel
  section: ServerSectionKind
  diagnostics?: BrowserDiagnosticJournal
}) {
  const { t, dateTime, number } = useI18n()
  const { instanceId } = useParams()
  const server = model.servers.find((item) => item.instanceId === instanceId)
  const [, setDiagnosticRevision] = useState(0)
  const [copied, setCopied] = useState(false)
  useEffect(() => diagnostics?.subscribe(() => setDiagnosticRevision((value) => value + 1)), [diagnostics])
  if (!server) return <Navigate to={'/' + section} replace />

  const diagnosticEntries = diagnostics?.list(server.instanceId) ?? []
  const title = t(titles[section])

  async function copyDiagnostics() {
    if (!diagnostics) return
    const text = diagnostics.exportText(server!.instanceId)
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
    <section className="stack" aria-labelledby="server-section-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{server.displayName}</p>
          <h2 id="server-section-title">{title}</h2>
          <p className="muted">{server.origin}</p>
        </div>
        <span className={'status fleet-status-' + server.freshness}>{server.freshness}</span>
      </div>

      <article className="panel">
        {section === 'health' ? (
          <>
            <h3>{t('section.lastKnownHealth')}</h3>
            <p>{server.healthy === false ? t('section.unhealthy') : server.healthy === true ? t('section.healthy') : t('common.unknown')}</p>
            <p className="muted">
              {t('section.connection')} {server.connectivity}
              {server.lastSeenAt ? ' · ' + t('section.lastActivity') + ' ' + dateTime(server.lastSeenAt) : ''}
            </p>
            <p className="muted">{t('server.hostResources')}: {server.resources?.status ?? t('common.unavailable')}</p>
            {server.staleReason ? <p className="muted">{server.staleReason}</p> : null}
            {server.lastError ? <p className="connection-error" role="status">{server.lastError}</p> : null}
          </>
        ) : section === 'context' ? (
          <>
            <h3>{title} {t('section.on')} {server.displayName}</h3>
            {(server.contexts ?? []).length === 0 ? <p className="muted">{t('context.empty')}</p> : (
              <div className="stack context-list">
                {(server.contexts ?? []).map((context) => (
                  <section className="context-entry" key={context.id}>
                    <div className="section-heading">
                      <strong>{context.summary}</strong>
                      <span className="chip">{context.primary ? t('context.primary') : t('context.additional')}</span>
                    </div>
                    {context.content ? <pre>{context.content}</pre> : null}
                  </section>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <h3>{title} {t('section.on')} {server.displayName}</h3>
            <p className="muted">{t('section.scopedDescription')}</p>
          </>
        )}
      </article>

      {section === 'health' && diagnostics ? (
        <details className="panel diagnostics-panel">
          <summary>{t('diagnostics.title')} · {number(diagnosticEntries.length)}</summary>
          <p className="muted">{t('diagnostics.description')}</p>
          <div className="diagnostics-actions">
            <button type="button" className="secondary-action" onClick={() => void copyDiagnostics()}>
              {copied ? t('diagnostics.copied') : t('diagnostics.copy')}
            </button>
          </div>
          {diagnosticEntries.length === 0 ? <p className="muted">{t('diagnostics.empty')}</p> : (
            <div className="diagnostics-log" aria-label={t('diagnostics.title')}>
              {diagnosticEntries.map((event) => (
                <div className="diagnostics-entry" key={event.seq}>
                  <div><time dateTime={event.at}>{dateTime(event.at)}</time> <code>#{event.seq} {event.type}</code></div>
                  <div className="muted">
                    {[event.server, event.status, event.authStatus ? `auth=${event.authStatus}` : '', event.code ? `code=${event.code}` : ''].filter(Boolean).join(' · ')}
                  </div>
                  {event.detail ? <div className="diagnostics-detail">{event.detail}</div> : null}
                </div>
              ))}
            </div>
          )}
        </details>
      ) : null}

      <Link className="text-link" to={'/servers/' + encodeURIComponent(server.instanceId)}>
        {t('section.openOverview')}
      </Link>
    </section>
  )
}
