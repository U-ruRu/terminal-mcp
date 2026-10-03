import { useMemo, useRef, useState } from 'react'

import type { FleetReadModel, FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import { ServerCard } from './ServerCard'
import { SegmentedControl } from './UiPrimitives'
import { needsAttention, serverAlphaSort, serverProblemSort, serverVisualState } from './serverPresentation'

type FleetFilter = 'all' | 'attention' | 'live'

export type FleetServerGroup = {
  key: string
  label?: string
  servers: FleetServerReadModel[]
}

export function FleetDashboard({ model, groups }: { model: FleetReadModel; groups?: FleetServerGroup[] }) {
  const { t, number } = useI18n()
  const [filter, setFilter] = useState<FleetFilter>('all')
  const serverListRef = useRef<HTMLDivElement>(null)
  const states = model.servers.map(serverVisualState)
  const problemCount = model.servers.filter(needsAttention).length
  const onlineCount = model.servers.filter((server) => serverVisualState(server) === 'healthy').length
  const criticalCount = states.filter((state) => state === 'offline' || state === 'critical').length
  const resourceAttentionCount = states.filter((state) => state === 'attention').length
  const staleCount = states.filter((state) => state === 'stale').length
  const fleetState = criticalCount > 0
    ? 'partial'
    : resourceAttentionCount > 0 || staleCount > 0
      ? 'attention'
      : states.some((state) => state === 'loading')
        ? 'loading'
        : 'healthy'

  const focusServerList = (nextFilter: FleetFilter) => {
    setFilter(nextFilter)
    serverListRef.current?.scrollIntoView?.({ block: 'start' })
    serverListRef.current?.focus({ preventScroll: true })
  }

  const visibleGroups = useMemo(() => {
    const source = groups ?? [{ key: 'all', servers: model.servers }]
    const filtered = source.map((group) => ({
      ...group,
      servers: group.servers
        .filter((server) => filter === 'attention' ? needsAttention(server) : filter === 'live' ? serverVisualState(server) === 'healthy' : true)
        .sort(filter === 'attention' ? serverProblemSort : serverAlphaSort),
    }))
    return filtered.filter((group) => group.servers.length > 0)
  }, [filter, groups, model.servers])

  const statusFilter: FleetFilter = fleetState === 'healthy' ? 'live' : fleetState === 'attention' || fleetState === 'partial' ? 'attention' : 'all'

  return (
    <section className="stack fleet-overview-primary" aria-label={t('nav.fleet')}>
      <button
        type="button"
        className={'fleet-status-card fleet-status-card-' + fleetState}
        aria-label={
          fleetState === 'partial'
            ? t('fleet.partialOffline') + ': ' + number(problemCount) + ' ' + t('fleet.servers')
            : fleetState === 'attention'
              ? t('fleet.problems') + ': ' + number(problemCount) + ' ' + t('fleet.servers')
              : fleetState === 'loading'
                ? t('status.catchingUp')
                : t('fleet.live') + ': ' + number(onlineCount) + ' ' + t('fleet.servers')
        }
        onClick={() => focusServerList(statusFilter)}
      >
        <span className="fleet-status-icon" aria-hidden="true">{fleetState === 'healthy' ? '✓' : fleetState === 'loading' ? '…' : '!'}</span>
        <span>
          <strong>{fleetState === 'partial' ? t('fleet.partialOffline') : fleetState === 'attention' ? t('fleet.problems') : fleetState === 'loading' ? t('status.catchingUp') : t('fleet.live')}</strong>
          <small>
            {number(model.summary.totalServers)} {t('fleet.servers')}
            {' · '}{number(criticalCount)} {t('fleet.offline')}
            {' · '}{number(problemCount)} {t('fleet.problems')}
            {' · '}{number(staleCount)} {t('fleet.stale')}
          </small>
        </span>
        <span className="fleet-status-chevron" aria-hidden="true">›</span>
      </button>

      <div className="fleet-server-filter">
        <SegmentedControl
          label={t('fleet.filterServers')}
          value={filter}
          onChange={(value) => setFilter(value as FleetFilter)}
          options={[
            { value: 'all', label: `${t('common.all')} (${number(model.servers.length)})` },
            { value: 'attention', label: `${t('fleet.problems')} (${number(problemCount)})` },
            { value: 'live', label: `${t('fleet.live')} (${number(onlineCount)})` },
          ]}
        />
      </div>

      <div ref={serverListRef} className="fleet-server-groups" tabIndex={-1} aria-label={t('nav.servers')}>
        {visibleGroups.map((group) => (
          <section className="fleet-server-group" key={group.key} aria-label={group.label}>
            {group.label ? <h3 className="fleet-group-title">{group.label}</h3> : null}
            <div className="fleet-grid fleet-grid-compact">
              {group.servers.map((server) => <ServerCard key={server.instanceId} server={server} />)}
            </div>
          </section>
        ))}
      </div>
    </section>
  )
}
