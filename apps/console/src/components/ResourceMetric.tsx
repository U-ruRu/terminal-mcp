import type { FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import {
  resourceDisplayValue,
  resourceVisualState,
  serverVisualState,
  type ResourceKind,
} from './serverPresentation'

export function ResourceMetric({
  server,
  kind,
  label,
}: {
  server: FleetServerReadModel
  kind: ResourceKind
  label: string
}) {
  const { t } = useI18n()
  const state = resourceVisualState(server, kind)
  const stale = serverVisualState(server) === 'stale'
  const value = resourceDisplayValue(server, kind, t('common.unavailable'), t('fleet.load'))

  return (
    <div className={'resource-metric resource-metric-' + state + (stale ? ' resource-metric-stale' : '')}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}
