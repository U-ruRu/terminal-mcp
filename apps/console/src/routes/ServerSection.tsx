import { Link, Navigate, useParams } from 'react-router-dom'

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
}: {
  model: FleetReadModel
  section: ServerSectionKind
}) {
  const { t, dateTime } = useI18n()
  const { instanceId } = useParams()
  const server = model.servers.find((item) => item.instanceId === instanceId)
  if (!server) return <Navigate to={'/' + section} replace />

  const title = t(titles[section])
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
          </>
        ) : (
          <>
            <h3>{title} {t('section.on')} {server.displayName}</h3>
            <p className="muted">
              {t('section.scopedDescription')}
            </p>
          </>
        )}
      </article>

      <Link className="text-link" to={'/servers/' + encodeURIComponent(server.instanceId)}>
        {t('section.openOverview')}
      </Link>
    </section>
  )
}
