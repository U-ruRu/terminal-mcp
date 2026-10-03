import type { FleetServerReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import {
  resourcePercent,
  resourceVisualState,
  type ResourceKind,
} from './serverPresentation'

function byteCapacity(bytes: number | undefined): string | undefined {
  if (bytes === undefined || !Number.isFinite(bytes) || bytes <= 0) return undefined
  const gib = bytes / (1024 ** 3)
  const value = gib >= 10 ? Math.round(gib) : Math.round(gib * 10) / 10
  return value + ' GB'
}

function capacity(server: FleetServerReadModel, kind: ResourceKind): string | undefined {
  if (!server.resources) return undefined
  if (kind === 'cpu') {
    const cores = server.resources.cpu.status === 'available' ? server.resources.cpu.logicalCores : undefined
    return cores ? cores + ' CPU' : undefined
  }
  const resource = kind === 'memory' ? server.resources.memory : server.resources.filesystem
  return resource.status === 'available' ? byteCapacity(resource.totalBytes) : undefined
}

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
  const percent = resourcePercent(server, kind)
  const available = state !== 'unavailable' && percent !== undefined
  const rawPercent = available ? Math.round(percent) : undefined
  const progress = available ? Math.max(0, Math.min(100, percent)) : undefined
  const total = available ? capacity(server, kind) : undefined

  return (
    <div className={'resource-metric resource-metric-' + state}>
      <dt>{label}</dt>
      <dd>
        {available ? (
          <>
            <span className="resource-metric-value">{rawPercent}%</span>
            {total ? <span className="resource-metric-capacity"> · {total}</span> : null}
          </>
        ) : (
          <span className="resource-metric-unavailable-value" aria-label={t('common.unavailable')}>—</span>
        )}
      </dd>
      {progress !== undefined ? (
        <div
          className="resource-progress"
          role="progressbar"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress)}
          data-raw-percent={rawPercent}
        >
          <span style={{ width: progress + '%' }} />
        </div>
      ) : null}
    </div>
  )
}
