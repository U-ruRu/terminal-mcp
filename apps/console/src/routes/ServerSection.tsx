import { useEffect, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'

import type { ContextReadModel } from '../api/models'
import type { BrowserDiagnosticJournal } from '../diagnostics/journal'
import type { FleetReadModel } from '../fleet/readModel'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState, IconButton, IconButtonRow } from '../components/UiPrimitives'

type ServerSectionKind = 'agents' | 'context' | 'health'

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
  const [contextQuery, setContextQuery] = useState('')
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
  const visibleContexts = contexts
    .filter((context) => {
      const query = contextQuery.trim().toLocaleLowerCase()
      if (!query) return true
      return context.summary.toLocaleLowerCase().includes(query) || (context.content ?? '').toLocaleLowerCase().includes(query)
    })
    .sort((left, right) => Number(right.primary) - Number(left.primary) || left.summary.localeCompare(right.summary))

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
    <section className="stack" aria-label={title}>
      <div className="server-context-status">
        <p className="muted">{server.origin}</p>
        <div className="server-state-strip" aria-label={server.displayName + ' state'}>
          <span className={'status server-status ' + (server.connectionState === 'live' ? 'server-status-healthy' : server.connectionState === 'offline' ? 'server-status-offline' : 'server-status-loading')}>{server.connectionState === 'live' ? t('status.live') : server.connectionState === 'offline' ? t('status.offline') : t('status.loading')}</span>
          <span className={'status server-status ' + (server.freshness === 'fresh' ? 'server-status-healthy' : server.freshness === 'stale' ? 'server-status-stale' : 'server-status-loading')}>{server.freshness === 'fresh' ? t('status.fresh') : server.freshness === 'stale' ? t('status.stale') : t('status.loading')}</span>
        </div>
      </div>

      <article className="panel">
        {section === 'health' ? (
          <>
            <h3>{t('section.healthDiagnostics')}</h3>
            <p className="muted">{t('diagnostics.description')}</p>
            {server.lastError ? <p className="connection-error" role="status">{diagnosticError(server.lastError, t)}</p> : null}
            {server.lastError || server.staleReason ? (
              <details className="inline-technical-details">
                <summary>{t('diagnostics.technicalDetails')}</summary>
                {server.lastError ? <code>{server.lastError}</code> : null}
                {server.staleReason ? <code>{server.staleReason}</code> : null}
              </details>
            ) : null}
          </>
        ) : section === 'context' ? (
          <>
            <div className="context-heading">
              <label className="context-search">
                <span className="visually-hidden">{t('context.search')}</span>
                <input type="search" value={contextQuery} onChange={(event) => setContextQuery(event.target.value)} placeholder={t('context.search')} />
              </label>
            </div>
            {contexts.length === 0 ? <FeedbackState variant="empty" title={t('context.empty')} /> : visibleContexts.length === 0 ? (
              <FeedbackState variant="empty" title={t('context.noMatches')} />
            ) : (
              <div className="context-list">
                {visibleContexts.map((context) => (
                  <section className={`context-entry ${context.primary ? 'context-entry-primary' : ''}`} key={context.id}>
                    <div className="section-heading">
                      <strong>{context.summary}</strong>
                      <span className="chip">{context.primary ? t('context.primary') : t('context.additional')}</span>
                    </div>
                    {context.content ? (
                      <details className="context-content">
                        <summary>{t('context.content')}</summary>
                        <pre>{context.content}</pre>
                      </details>
                    ) : null}
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
          <IconButtonRow className="diagnostics-actions">
            <IconButton icon="delete" variant="destructive" label={t('diagnostics.clear')} onClick={() => diagnostics.clear()} disabled={diagnosticEntries.length === 0} />
            <IconButton icon="copy" variant="secondary" label={copied ? t('diagnostics.copied') : t('diagnostics.copy')} onClick={() => void copyDiagnostics()} disabled={diagnosticEntries.length === 0} />
          </IconButtonRow>
          {diagnosticEntries.length === 0 ? <FeedbackState variant="empty" title={t('diagnostics.empty')} /> : (
            <div className="diagnostics-log" aria-label={t('diagnostics.title')}>
              {diagnosticEntries.map((event) => (
                <div className="diagnostics-entry" key={event.seq}>
                  <div className="diagnostics-entry-heading">
                    <time dateTime={event.at}>{dateTime(event.at)}</time>
                    <strong>{event.code ? diagnosticError(event.code, t) : event.status ? connectionLabel(event.status, t) : t('diagnostics.connectionError')}</strong>
                  </div>
                  <details className="inline-technical-details">
                    <summary>{t('diagnostics.technicalDetails')}</summary>
                    <code>#{event.seq} {event.type}</code>
                    <div className="muted">
                      {[event.server, event.status, event.authStatus ? `auth=${event.authStatus}` : '', event.code ? `code=${event.code}` : ''].filter(Boolean).join(' · ')}
                    </div>
                    {event.detail ? <div className="diagnostics-detail">{event.detail}</div> : null}
                  </details>
                </div>
              ))}
            </div>
          )}
        </details>
      ) : null}

    </section>
  )
}
