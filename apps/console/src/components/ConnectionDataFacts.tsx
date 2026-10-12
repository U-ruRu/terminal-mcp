import type { ControlObservation } from '../connections/controlObservation'
import type { FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'

/** Each label states its own observation scope; authentication is not telemetry freshness. */
export function ConnectionDataFacts({ server, control, canConfigure }: {
  server?: FleetServerReadModel
  control?: ControlObservation
  canConfigure: boolean
}) {
  const { t } = useI18n()
  const availableResources = Boolean(server?.resources && [server.resources.cpu, server.resources.memory, server.resources.filesystem].some((item) => item.status === 'available'))
  const resourceLabel = !availableResources ? t('common.unavailable') : server?.freshness === 'fresh'
    ? server.resources?.status === 'partial' ? t('connections.resourcePartial') : t('status.fresh') : t('status.stale')
  return <div className="connection-fleet-status connection-data-facts">
    <span>{t('connections.serverAvailability')}: {server?.connectionState === 'live' ? t('connections.lifecycle.live') : server?.connectionState === 'offline' ? t('connections.lifecycle.offline') : t('connections.unknown')}</span>
    <span>{t('connections.resourceFreshness')}: {resourceLabel}</span>
    <span>{t('connections.controlState')}: {control?.freshness === 'fresh' && !control.error ? t('status.fresh') : control?.control ? t('status.stale') : t('common.unavailable')}</span>
    <span>{t('connections.configurationReady')}: {t(canConfigure ? 'connections.ready' : 'connections.notReady')}</span>
  </div>
}
