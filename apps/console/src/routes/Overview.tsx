import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import type { ManagedFleetControlReadModel } from '../api/models'
import { FleetDashboard } from '../components/FleetDashboard'
import { FeedbackState } from '../components/UiPrimitives'
import type { FleetReadModel } from '../fleet/readModel'
import { useI18n } from '../i18n/useI18n'
import { meshRoute } from '../navigation/routes'

type MeshObservation = {
  instanceId: string
  control?: ManagedFleetControlReadModel
  unavailable?: boolean
}

export function Overview({
  model,
  loadFleetControl,
}: {
  model: FleetReadModel
  loadFleetControl?: (instanceId: string) => Promise<ManagedFleetControlReadModel>
}) {
  const { t, number } = useI18n()
  const [observations, setObservations] = useState<MeshObservation[]>([])

  useEffect(() => {
    if (!loadFleetControl || model.servers.length === 0) return
    let cancelled = false
    void Promise.all(model.servers.map(async (server): Promise<MeshObservation> => {
      try {
        return { instanceId: server.instanceId, control: await loadFleetControl(server.instanceId) }
      } catch {
        return { instanceId: server.instanceId, unavailable: true }
      }
    })).then((values) => {
      if (!cancelled) setObservations(values)
    })
    return () => { cancelled = true }
  }, [loadFleetControl, model.servers])

  const meshes = useMemo(() => {
    const byId = new Map<string, { meshId: string; displayName: string; members: number; revision: number }>()
    for (const observation of observations) {
      const control = observation.control
      if (!control?.managed) continue
      for (const mesh of control.meshes) {
        const candidate = {
          meshId: mesh.meshId,
          displayName: mesh.displayName,
          members: control.nodes.filter((node) => node.state !== 'detached' && node.meshId === mesh.meshId).length,
          revision: control.revisions.topology,
        }
        const current = byId.get(mesh.meshId)
        if (!current || candidate.revision > current.revision) byId.set(mesh.meshId, candidate)
      }
    }
    return Array.from(byId.values()).sort((left, right) => left.displayName.localeCompare(right.displayName) || left.meshId.localeCompare(right.meshId))
  }, [observations])

  const loading = Boolean(loadFleetControl) && model.servers.length > 0 && observations.length === 0
  const unavailable = observations.length > 0 && observations.every((item) => !item.control)

  return (
    <div className="stack">
      <FleetDashboard model={model} />
      <section className="panel fleet-mesh-index" aria-labelledby="fleet-mesh-title">
        <div>
          <p className="eyebrow">{t('connections.mesh')}</p>
          <h3 id="fleet-mesh-title">{t('fleet.meshes')}</h3>
          <p className="muted">{t('fleet.meshesHint')}</p>
        </div>
        {loading ? (
          <FeedbackState variant="loading" title={t('status.catchingUp')} />
        ) : unavailable ? (
          <FeedbackState variant="partial" title={t('connections.unknown')} />
        ) : meshes.length === 0 ? (
          <FeedbackState variant="empty" title={t('fleet.noMeshes')} />
        ) : (
          <div className="fleet-mesh-list">
            {meshes.map((mesh) => (
              <Link className="fleet-mesh-row" to={meshRoute(mesh.meshId)} key={mesh.meshId}>
                <span>
                  <strong>{mesh.displayName}</strong>
                  <small>{mesh.meshId}</small>
                </span>
                <span className="muted">{number(mesh.members)} {t('fleet.servers')}</span>
                <span aria-hidden="true">›</span>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
