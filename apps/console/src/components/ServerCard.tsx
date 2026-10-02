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

type ServerCardProps = {
  server: FleetServerReadModel
  variant?: 'compact' | 'large'
  interactive?: boolean
}

export function ServerCard({
  server,
  variant = 'compact',
  interactive = variant === 'compact',
}: ServerCardProps) {
  const { t, number } = useI18n()
  const state = serverVisualState(server)

  const content = (
    <>
      {variant === 'compact' ? (
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
      ) : null}

      <dl className="server-card-metrics">
        <ResourceMetric server={server} kind="cpu" label={t('common.cpu')} />
        <ResourceMetric server={server} kind="memory" label={t('common.ram')} />
        <ResourceMetric server={server} kind="filesystem" label={t('common.disk')} />
      </dl>

      {variant === 'large' ? (
        <div className="server-card-large-summary">
          <div className="server-card-agent-count">
            <span>{t('server.agents')}</span>
            <strong>{number(server.activeAgentCount)}</strong>
          </div>
          <dl className="server-card-task-summary" aria-label={t('server.summary')}>
            <div><dt>{t('fleet.ready')}</dt><dd>{number(server.taskCounts.ready)}</dd></div>
            <div><dt>{t('fleet.blocked')}</dt><dd>{number(server.taskCounts.blocked)}</dd></div>
            <div><dt>{t('tasks.completed')}</dt><dd>{number(server.taskCounts.done)}</dd></div>
          </dl>
          <div className="server-card-communication">
            <span>{t('server.alerts')} <strong>{number(server.communication.alerts)}</strong></span>
            <span>{t('server.replies')} <strong>{number(server.communication.replyRequired)}</strong></span>
          </div>
        </div>
      ) : null}

      {state === 'stale' && server.lastSeenAt ? (
        <p className="server-card-age">{server.lastSeenAt.replace('T', ' ').replace('Z', ' UTC')}</p>
      ) : null}
      {interactive ? <span className="server-card-chevron" aria-hidden="true">›</span> : null}
    </>
  )

  return (
    <article
      className={'server-card server-card-' + variant + ' server-card-' + state}
      data-state={state}
      data-variant={variant}
      aria-label={server.displayName + ' ' + t('fleet.serverSuffix')}
    >
      {interactive ? (
        <Link
          className="server-card-link"
          to={'/servers/' + encodeURIComponent(server.instanceId)}
          aria-label={server.displayName + ' · ' + statusLabel(state, t)}
        >
          {content}
        </Link>
      ) : (
        <div className="server-card-link server-card-static">{content}</div>
      )}
    </article>
  )
}
