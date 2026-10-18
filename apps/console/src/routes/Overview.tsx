import { coalesceControlReads } from '../fleet/controlRead'
import { ServerSlots, type ServerSlotsProps } from './ServerSlots'
import { useEffect, useMemo, useState } from 'react'
import { Icon } from '../components/Icon'
import { Link } from 'react-router-dom'

import type { ManagedFleetControlReadModel } from '../api/models'
import { observedMembership } from '../connections/membershipReadiness'
import { FleetDashboard, type FleetServerGroup } from '../components/FleetDashboard'
import { FeedbackState } from '../components/UiPrimitives'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { meshRoute } from '../navigation/routes'

type MeshObservation = { instanceId: string; control?: ManagedFleetControlReadModel; unavailable?: boolean }
type Membership = { meshId: string; label: string }

export function Overview({ model, instances = [], loadFleetControl: sourceControlLoader, loadAccessMeshSlots, loadAccessMeshDefaults, mutateAccessMesh }: {
  loadAccessMeshSlots?: ServerSlotsProps['loadAccessMeshSlots']
  loadAccessMeshDefaults?: ServerSlotsProps['loadAccessMeshDefaults']
  mutateAccessMesh?: ServerSlotsProps['mutateAccessMesh']
  model: FleetReadModel
  instances?: FleetInstanceView[]
  loadFleetControl?: (instanceId: string) => Promise<ManagedFleetControlReadModel>
}) {
  const { t, number } = useI18n()
  const [observations, setObservations] = useState<MeshObservation[]>([])
  const loadFleetControl = useMemo(() => sourceControlLoader ? coalesceControlReads(sourceControlLoader) : undefined, [sourceControlLoader])

  useEffect(() => {
    if (!loadFleetControl || model.servers.length === 0) return
    let cancelled = false
    for (const server of model.servers) {
      void loadFleetControl(server.instanceId).then((control) => ({ instanceId: server.instanceId, control }), () => ({ instanceId: server.instanceId, unavailable: true }))
        .then((value) => { if (!cancelled) setObservations((current) => [...current.filter((item) => item.instanceId !== value.instanceId), value]) })
    }
    return () => { cancelled = true }
  }, [loadFleetControl, model.servers])

  const topology = useMemo(() => {
    const meshes = new Map<string, { meshId: string; displayName: string; members: number; revision: number }>()
    const memberships = new Map<string, Membership>()
    const meshLabels = new Map<string, string>()

    for (const observation of observations) {
      const control = observation.control
      if (!control?.managed) continue
      for (const mesh of control.meshes) meshLabels.set(mesh.meshId, mesh.displayName)
      if (control.mesh) meshLabels.set(control.mesh.meshId, control.mesh.displayName)
      for (const mesh of control.meshes) {
        const candidate = { meshId: mesh.meshId, displayName: mesh.displayName, members: 0, revision: control.revisions.topology }
        const current = meshes.get(mesh.meshId)
        if (!current || candidate.revision > current.revision) meshes.set(mesh.meshId, candidate)
      }
      const local = observedMembership({ control, freshness: 'fresh' })
      if (local.kind === 'mesh' && local.meshId && model.servers.some((server) => server.instanceId === observation.instanceId)) {
        memberships.set(observation.instanceId, { meshId: local.meshId, label: meshLabels.get(local.meshId) ?? local.meshId })
      }
    }
    for (const member of memberships.values()) {
      member.label = meshLabels.get(member.meshId) ?? member.label
      const mesh = meshes.get(member.meshId)
      if (mesh) mesh.members++
    }
    return { meshes: Array.from(meshes.values()).sort((a, b) => a.displayName.localeCompare(b.displayName) || a.meshId.localeCompare(b.meshId)), memberships }
  }, [model.servers, observations])

  const serverGroups = useMemo<FleetServerGroup[]>(() => {
    const byMesh = new Map<string, FleetServerGroup>()
    const standalone: FleetServerGroup = { key: 'standalone', label: t('connections.standalone'), servers: [] }
    const unknown: FleetServerGroup = { key: 'unknown', label: t('connections.unknown'), servers: [] }
    for (const server of model.servers) {
      const membership = topology.memberships.get(server.instanceId)
      if (!membership) {
        const observed = observations.find((item) => item.instanceId === server.instanceId)
        const local = observedMembership({ control: observed?.control, freshness: observed?.control ? 'fresh' : 'unknown' })
        const group = local.kind === 'standalone' ? standalone : unknown
        group.servers.push(server)
        continue
      }
      const key = `mesh:${membership.meshId}`
      const group = byMesh.get(key) ?? { key, label: membership.label, servers: [] }
      group.servers.push(server)
      byMesh.set(key, group)
    }
    const groups = Array.from(byMesh.values()).sort((a, b) => (a.label ?? '').localeCompare(b.label ?? ''))
    if (standalone.servers.length) groups.push(standalone)
    if (unknown.servers.length) groups.push(unknown)
    return groups
  }, [model.servers, observations, t, topology.memberships])

  const loading = Boolean(loadFleetControl) && model.servers.length > 0 && observations.length === 0
  const unavailable = observations.length > 0 && observations.every((item) => !item.control)

  return (
    <div className="stack fleet-overview">
      <FleetDashboard model={model} groups={serverGroups} />

      <section className="fleet-agent-sessions" aria-labelledby="fleet-agent-sessions-title">
        <h2 id="fleet-agent-sessions-title" className="fleet-section-title">{t('fleet.agentSessions')}</h2>
        <ServerSlots activeSessionsOnly instances={instances} loadAccessMeshSlots={loadAccessMeshSlots} loadAccessMeshDefaults={loadAccessMeshDefaults} mutateAccessMesh={mutateAccessMesh} loadFleetControl={loadFleetControl} />
      </section>

      <section className="fleet-mesh-index" aria-labelledby="fleet-mesh-title">
        <h2 id="fleet-mesh-title" className="fleet-section-title">{t('fleet.meshes')}</h2>
        {loading ? <FeedbackState variant="loading" title={t('status.catchingUp')} /> : unavailable ? <FeedbackState variant="partial" title={t('connections.unknown')} /> : topology.meshes.length === 0 ? <FeedbackState variant="empty" title={t('fleet.noMeshes')} /> : (
          <div className="fleet-mesh-list">{topology.meshes.map((mesh) => (
            <Link className="fleet-mesh-row" to={meshRoute(mesh.meshId)} key={mesh.meshId}><span className="fleet-mesh-identity"><Icon name="mesh" /><strong>{mesh.displayName}</strong></span><span className="muted">{number(mesh.members)} {t('fleet.servers')}</span><span className="fleet-mesh-chevron" aria-hidden="true"><Icon name="chevron-right" /></span></Link>
          ))}</div>
        )}
      </section>
    </div>
  )
}
