import { useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'

import type { ContextReadModel } from '../api/models'
import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import type { FleetReadModel } from '../fleet/readModel'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'

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
  loadContexts,
}: {
  model: FleetReadModel
  section: ServerSectionKind
  diagnostics?: BrowserDiagnosticJournal
  loadContexts?: (instanceId: string) => Promise<ContextReadModel[]>
}) {
  const { t, dateTime, number } = useI18n()
  const { instanceId } = useParams()
  const server = model.servers.find((item) => item.instanceId === instanceId)
  const [, setDiagnosticRevision] = useState(0)
  const [copied, setCopied] = useState(false)
  const [loadedContexts, setLoadedContexts] = useState<{
    instanceId: string
    values?: ContextReadModel[]
    error?: string
  }>({ instanceId: '' })
  useEffect(() => diagnostics?.subscribe(() => setDiagnosticRevision((value) => value + 1)), [diagnostics])
  useEffect(() => {
    if (section !== 'context' || !instanceId || !loadContexts) return
    let cancelled = false
    void loadContexts(instanceId)
      .then((values) => {
        if (!cancelled) setLoadedContexts({ instanceId, values })
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadedContexts({
            instanceId,
            error: error instanceof Error ? error.message : 'context_unavailable',
          })
        }
      })
    return () => { cancelled = true }
  }, [instanceId, loadContexts, section])
  if (!server) return <Navigate to={'/' + section} replace />

  const diagnosticEntries = diagnostics?.list(server.instanceId) ?? []
  const title = t(titles[section])
  const contexts = loadedContexts.instanceId === server.instanceId && loadedContexts.values
    ? loadedContexts.values
    : (server.contexts ?? [])
  const contextError = loadedContexts.instanceId === server.instanceId
    ? loadedContexts.error
    : undefined

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
            {contexts.length === 0 ? <FeedbackState variant="empty" title={t('context.empty')} /> : (
              <div className="stack context-list">
                {contexts.map((context) => (
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
            {contextError ? <FeedbackState variant="partial" title={t('activity.unavailable')} detail={contextError} /> : null}
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
            <button type="button" className="secondary-action" onClick={() => diagnostics.clear()} disabled={diagnosticEntries.length === 0}>
              {t('diagnostics.clear')}
            </button>
            <button type="button" className="secondary-action" onClick={() => void copyDiagnostics()} disabled={diagnosticEntries.length === 0}>
              {copied ? t('diagnostics.copied') : t('diagnostics.copy')}
            </button>
          </div>
          {diagnosticEntries.length === 0 ? <FeedbackState variant="empty" title={t('diagnostics.empty')} /> : (
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
