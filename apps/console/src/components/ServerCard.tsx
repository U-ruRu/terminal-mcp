import { Link } from 'react-router-dom'

import type { FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import { ResourceMetric } from './ResourceMetric'
import { StatusBadge } from './StatusBadge'
import { serverVisualState, type ServerVisualState } from './serverPresentation'

function statusLabel(state: ServerVisualState, t: ReturnType<typeof useI18n>['t']): string {
  if (state === 'healthy') return t('status.live')
  if (state === 'stale') return t('status.stale')
  if (state === 'offline') return t('status.offline')
  if (state === 'loading') return t('status.catchingUp')
  return t('fleet.needsAttention')
}

export function ServerCard({ server }: { server: FleetServerReadModel }) {
  const { t } = useI18n()
  const state = serverVisualState(server)

  return (
    <article
      className={'server-card server-card-compact server-card-' + state}
      data-state={state}
      aria-label={server.displayName + ' ' + t('fleet.serverSuffix')}
    >
      <Link
        className="server-card-link"
        to={'/servers/' + encodeURIComponent(server.instanceId)}
        aria-label={server.displayName + ' · ' + statusLabel(state, t)}
      >
        <div className="server-card-heading">
          <div className="server-card-identity">
            <span className="server-card-icon" aria-hidden="true">▣</span>
            <div>
              <h3>{server.displayName}</h3>
              <p>{server.version ?? t('fleet.versionUnavailable')}</p>
            </div>
          </div>
          <StatusBadge state={state} label={statusLabel(state, t)} />
        </div>

        <dl className="server-card-metrics">
          <ResourceMetric server={server} kind="cpu" label={t('common.cpu')} />
          <ResourceMetric server={server} kind="memory" label={t('common.ram')} />
          <ResourceMetric server={server} kind="filesystem" label={t('common.disk')} />
        </dl>

        {state === 'stale' && server.lastSeenAt ? (
          <p className="server-card-age">{server.lastSeenAt.replace('T', ' ').replace('Z', ' UTC')}</p>
        ) : null}
        <span className="server-card-chevron" aria-hidden="true">›</span>
      </Link>
    </article>
  )
}
