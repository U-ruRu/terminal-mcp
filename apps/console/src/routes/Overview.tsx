import { coalesceControlReads } from '../fleet/controlRead'
import { ServerSlots, type ServerSlotsProps } from './ServerSlots'
import { useEffect, useMemo, useState } from 'react'
import { Icon } from '../components/Icon'
import { Link } from 'react-router-dom'

import type { ManagedFleetControlReadModel } from '../api/models'
import { FleetDashboard, type FleetServerGroup } from '../components/FleetDashboard'
import { FeedbackState } from '../components/UiPrimitives'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { meshRoute } from '../navigation/routes'

type MeshObservation = { instanceId: string; control?: ManagedFleetControlReadModel; unavailable?: boolean }
type Membership = { meshId: string; label: string; revision: number }

function normalizedOrigin(value: string | undefined): string { return (value ?? '').replace(/\/+$/, '').toLowerCase() }

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
    const serverByOrigin = new Map(model.servers.map((server) => [normalizedOrigin(server.origin), server.instanceId]))

    for (const observation of observations) {
      const control = observation.control
      if (!control?.managed) continue
      for (const mesh of control.meshes) meshLabels.set(mesh.meshId, mesh.displayName)
      if (control.mesh) meshLabels.set(control.mesh.meshId, control.mesh.displayName)
      for (const mesh of control.meshes) {
        const candidate = { meshId: mesh.meshId, displayName: mesh.displayName, members: control.nodes.filter((node) => node.state !== 'detached' && node.meshId === mesh.meshId).length, revision: control.revisions.topology }
        const current = meshes.get(mesh.meshId)
        if (!current || candidate.revision > current.revision) meshes.set(mesh.meshId, candidate)
      }
      for (const node of control.nodes) {
        if (!node.meshId || node.state === 'detached') continue
        const instanceId = model.servers.some((server) => server.instanceId === node.nodeId) ? node.nodeId : serverByOrigin.get(normalizedOrigin(node.origin))
        if (!instanceId) continue
        const next = { meshId: node.meshId, label: meshLabels.get(node.meshId) ?? node.meshId, revision: control.revisions.topology }
        const current = memberships.get(instanceId)
        if (!current || next.revision > current.revision) memberships.set(instanceId, next)
      }
      const localServer = model.servers.find((server) => server.instanceId === observation.instanceId)
      const localNode = localServer ? control.nodes.find((node) => node.nodeId === control.nodeId || normalizedOrigin(node.origin) === normalizedOrigin(localServer.origin)) : undefined
      const localMeshId = localNode?.meshId ?? control.mesh?.meshId
      if (localServer && localMeshId && localNode?.state !== 'detached') {
        const next = { meshId: localMeshId, label: meshLabels.get(localMeshId) ?? localMeshId, revision: control.revisions.topology }
        const current = memberships.get(localServer.instanceId)
        if (!current || next.revision > current.revision) memberships.set(localServer.instanceId, next)
      }
    }
    for (const member of memberships.values()) member.label = meshLabels.get(member.meshId) ?? member.label
    return { meshes: Array.from(meshes.values()).sort((a, b) => a.displayName.localeCompare(b.displayName) || a.meshId.localeCompare(b.meshId)), memberships }
  }, [model.servers, observations])

  const serverGroups = useMemo<FleetServerGroup[]>(() => {
    const byMesh = new Map<string, FleetServerGroup>()
    const standalone: FleetServerGroup = { key: 'standalone', label: t('connections.standalone'), servers: [] }
    for (const server of model.servers) {
      const membership = topology.memberships.get(server.instanceId)
      if (!membership) { standalone.servers.push(server); continue }
      const key = `mesh:${membership.meshId}`
      const group = byMesh.get(key) ?? { key, label: membership.label, servers: [] }
      group.servers.push(server)
      byMesh.set(key, group)
    }
    const groups = Array.from(byMesh.values()).sort((a, b) => (a.label ?? '').localeCompare(b.label ?? ''))
    if (standalone.servers.length) groups.push(standalone)
    return groups
  }, [model.servers, t, topology.memberships])

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
