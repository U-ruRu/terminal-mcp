import { Link } from 'react-router-dom'

import type { FleetReadModel } from '../fleet/readModel'
import type { MessageKey } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { FeedbackState } from '../components/UiPrimitives'

export type ServerDestination = 'agents' | 'slots' | 'tasks' | 'context' | 'health'

const labels: Record<ServerDestination, MessageKey> = {
  agents: 'nav.agents',
  slots: 'nav.slots',
  tasks: 'nav.tasks',
  context: 'nav.context',
  health: 'nav.health',
}

function destination(instanceId: string, section: ServerDestination): string {
  return '/servers/' + encodeURIComponent(instanceId) + '/' + section
}

export function ServerChooser({
  model,
  section,
}: {
  model: FleetReadModel
  section: ServerDestination
}) {
  const { t } = useI18n()
  const label = t(labels[section])
  return (
    <section className="stack" aria-labelledby="server-choice-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t('chooser.eyebrow')}</p>
          <h2 id="server-choice-title">{t('chooser.chooseServerFor')} {label}</h2>
          <p className="muted">{t('chooser.explicit')}</p>
        </div>
      </div>

      <div className="server-choice-grid">
        {model.servers.map((server) => (
          <Link
            className="server-choice"
            key={server.instanceId}
            to={destination(server.instanceId, section)}
          >
            <strong>{server.displayName}</strong>
            <span>{server.connectivity}</span>
            <small>{server.origin}</small>
          </Link>
        ))}
      </div>

      {model.servers.length === 0 ? (
        <FeedbackState variant="empty" title={t('chooser.noServers')} />
      ) : null}
    </section>
  )
}
