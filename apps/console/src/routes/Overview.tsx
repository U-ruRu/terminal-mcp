import { useEffect, useMemo, useState } from 'react'
import { Icon } from '../components/Icon'
import { Link } from 'react-router-dom'

import type { ManagedFleetControlReadModel, PersistentMutationResult, PersistentSlotReadModel } from '../api/models'
import { clearAccessCode, loadAccessCode, saveAccessCode } from '../access/codeVault'
import { FleetDashboard, type FleetServerGroup } from '../components/FleetDashboard'
import { FeedbackState } from '../components/UiPrimitives'
import type { FleetReadModel } from '../fleet/readModel'
import type { FleetInstanceView } from '../fleet/types'
import { useI18n } from '../i18n/useI18n'
import { meshPersistentSlotRoute, meshRoute, slotRoute } from '../navigation/routes'

type MeshObservation = { instanceId: string; control?: ManagedFleetControlReadModel; unavailable?: boolean }
type Membership = { meshId: string; label: string; revision: number }
type ActiveSlotEntry = { slot: PersistentSlotReadModel; instanceId: string; groupKey: string; groupLabel: string; meshId?: string; authorityMatch: boolean }

function normalizedOrigin(value: string | undefined): string { return (value ?? '').replace(/\/+$/, '').toLowerCase() }
function idempotencyKey(): string { return globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `console-${Date.now()}-${Math.random().toString(36).slice(2)}` }

export function Overview({ model, instances = [], loadFleetControl, mutatePersistent }: {
  model: FleetReadModel
  instances?: FleetInstanceView[]
  loadFleetControl?: (instanceId: string) => Promise<ManagedFleetControlReadModel>
  mutatePersistent?: (instanceId: string, path: string, body: Record<string, unknown>) => Promise<PersistentMutationResult>
}) {
  const { t, number, locale } = useI18n()
  const [observations, setObservations] = useState<MeshObservation[]>([])
  const [clockOrigin] = useState(() => Date.now())
  const [clock, setClock] = useState(() => Date.now())
  const [slotMessage, setSlotMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [, setAccessRevision] = useState(0)

  useEffect(() => {
    if (!loadFleetControl || model.servers.length === 0) return
    let cancelled = false
    void Promise.all(model.servers.map(async (server): Promise<MeshObservation> => {
      try { return { instanceId: server.instanceId, control: await loadFleetControl(server.instanceId) } }
      catch { return { instanceId: server.instanceId, unavailable: true } }
    })).then((values) => { if (!cancelled) setObservations(values) })
    return () => { cancelled = true }
  }, [loadFleetControl, model.servers])

  useEffect(() => {
    const handle = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(handle)
  }, [])

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

  const activeSlotGroups = useMemo(() => {
    const entries = new Map<string, ActiveSlotEntry>()
    for (const instance of instances) {
      const persistent = instance.runtime.realtime?.snapshot?.persistent
      if (!persistent?.enabled || !persistent.available) continue
      const membership = topology.memberships.get(instance.profile.instanceId)
      const groupKey = membership ? `mesh:${membership.meshId}` : `server:${instance.profile.instanceId}`
      const groupLabel = membership?.label ?? instance.profile.displayName
      const control = observations.find((item) => item.instanceId === instance.profile.instanceId)?.control
      for (const slot of persistent.slots ?? []) {
        if (!slot.workSession || !['active', 'stopping'].includes(slot.state) || !['active', 'stopping'].includes(slot.workSession.state)) continue
        const slotServerNow = Date.parse(slot.serverNow)
        const anchoredNow = Number.isFinite(slotServerNow) ? slotServerNow + (clock - clockOrigin) : clock
        if (!Number.isFinite(Date.parse(slot.workSession.hardExpiresAt)) || Date.parse(slot.workSession.hardExpiresAt) <= anchoredNow) continue
        const authorityMatch = slot.authorityNodeId === instance.profile.instanceId || slot.authorityNodeId === control?.nodeId
        const key = `${groupKey}:${slot.logicalAgentId}`
        const current = entries.get(key)
        const candidate = { slot, instanceId: instance.profile.instanceId, groupKey, groupLabel, meshId: membership?.meshId, authorityMatch }
        if (!current || (authorityMatch && !current.authorityMatch) || (authorityMatch === current.authorityMatch && slot.slotRevision > current.slot.slotRevision)) entries.set(key, candidate)
      }
    }
    const groups = new Map<string, { key: string; label: string; meshId?: string; entries: ActiveSlotEntry[] }>()
    for (const entry of entries.values()) {
      const group = groups.get(entry.groupKey) ?? { key: entry.groupKey, label: entry.groupLabel, meshId: entry.meshId, entries: [] }
      group.entries.push(entry); groups.set(entry.groupKey, group)
    }
    return Array.from(groups.values()).sort((a, b) => Number(!a.meshId) - Number(!b.meshId) || a.label.localeCompare(b.label)).map((group) => ({ ...group, entries: group.entries.sort((a, b) => (a.slot.access?.publicName ?? a.slot.displayName).localeCompare(b.slot.access?.publicName ?? b.slot.displayName)) }))
  }, [clock, clockOrigin, instances, observations, topology.memberships])

  const loading = Boolean(loadFleetControl) && model.servers.length > 0 && observations.length === 0
  const unavailable = observations.length > 0 && observations.every((item) => !item.control)

  const slotName = (slot: PersistentSlotReadModel) => {
    const publicName = slot.access?.publicName?.trim()
    const displayName = slot.displayName.trim()
    return publicName && publicName !== slot.logicalAgentId ? (displayName && displayName !== publicName && displayName !== slot.logicalAgentId ? `${publicName} · ${displayName}` : publicName) : (displayName && displayName !== slot.logicalAgentId ? displayName : t('slots.identityUnavailable'))
  }
  const slotHref = (entry: ActiveSlotEntry) => entry.meshId ? meshPersistentSlotRoute(entry.meshId, entry.slot.logicalAgentId) : slotRoute(entry.instanceId, entry.slot.logicalAgentId)
  const timer = (slot: PersistentSlotReadModel) => {
    const serverBase = Date.parse(slot.serverNow)
    const anchoredNow = Number.isFinite(serverBase) ? serverBase + (clock - clockOrigin) : clock
    const remaining = Math.max(0, Math.floor((Date.parse(slot.workSession?.hardExpiresAt ?? '') - anchoredNow) / 1000))
    const h = Math.floor(remaining / 3600), m = Math.floor((remaining % 3600) / 60), s = remaining % 60
    const suffix = locale === 'ru' ? ['ч','м','с'] : ['h','m','s']
    return h ? `${h}${suffix[0]} ${String(m).padStart(2,'0')}${suffix[1]} ${String(s).padStart(2,'0')}${suffix[2]}` : m ? `${m}${suffix[1]} ${String(s).padStart(2,'0')}${suffix[2]}` : `${s}${suffix[2]}`
  }
  const mutateSlot = async (entry: ActiveSlotEntry, action: 'suspend' | 'delete' | 'rotate') => {
    if (!mutatePersistent) { setSlotMessage(t('slots.liveRequired')); return }
    const instance = instances.find((item) => item.profile.instanceId === entry.instanceId)
    if (instance?.runtime.authStatus !== 'connected') { setSlotMessage(t('slots.liveRequired')); return }
    const key = `${entry.slot.logicalAgentId}:${action}`; setBusy(key); setSlotMessage('')
    try {
      const path = action === 'rotate' ? '/actions/persistent/slots/rotate-access-code' : `/actions/persistent/slots/${action}`
      const body = action === 'rotate' ? { logical_agent_id: entry.slot.logicalAgentId } : { logical_agent_id: entry.slot.logicalAgentId, expected_revision: entry.slot.slotRevision, idempotency_key: idempotencyKey() }
      const result = await mutatePersistent(entry.instanceId, path, body)
      if (!result.ok) { setSlotMessage(result.code ?? result.error ?? t('slots.mutationFailed')); return }
      if (action === 'delete') { clearAccessCode(entry.slot.logicalAgentId); setAccessRevision((value) => value + 1) }
      if (action === 'rotate') {
        const access = result.payload.access as Record<string, unknown> | undefined
        const code = typeof access?.access_code === 'string' ? access.access_code : ''
        const generation = Number.isInteger(access?.access_generation) ? Number(access?.access_generation) : 0
        const publicName = typeof access?.public_name === 'string' ? access.public_name : undefined
        if (/^[0-9]{4}$/.test(code) && generation > 0) { saveAccessCode(entry.slot.logicalAgentId, { code, generation, publicName }); setAccessRevision((value) => value + 1); try { await navigator.clipboard.writeText(code) } catch { /* keep rotated code in vault */ } }
      }
      setSlotMessage(t('slots.mutationApplied'))
    } catch (error) { setSlotMessage(error instanceof Error ? error.message : t('slots.mutationFailed')) }
    finally { setBusy('') }
  }

  return (
    <div className="stack fleet-overview">
      <FleetDashboard model={model} groups={serverGroups} />

      <section className="fleet-agent-sessions" aria-labelledby="fleet-agent-sessions-title">
        <h2 id="fleet-agent-sessions-title" className="fleet-section-title">{t('fleet.agentSessions')}</h2>
        {slotMessage ? <div className="attention-strip" role="status">{slotMessage}</div> : null}
        {activeSlotGroups.length === 0 ? <FeedbackState variant="empty" title={t('fleet.noActiveSharedSessions')} /> : activeSlotGroups.map((group) => (
          <section className="fleet-session-group" key={group.key} aria-label={group.label}>
            <h3 className="fleet-group-title">{group.label}</h3>
            <div className="slot-grid fleet-slot-grid">{group.entries.map((entry) => {
              const saved = loadAccessCode(entry.slot.logicalAgentId, entry.slot.access?.accessGeneration ?? 0)
              return <article className="panel slot-card slot-cue-normal" key={`${group.key}:${entry.slot.logicalAgentId}`}>
                <div className="slot-card-primary">
                  <Link className="text-link slot-card-identity" to={slotHref(entry)}><strong>{slotName(entry.slot)}</strong></Link>
                  <span className="slot-timer">{timer(entry.slot)}</span>
                  <span className="chip">{entry.slot.state === 'stopping' ? t('slots.state.stopping') : t('slots.state.active')}</span>
                </div>
                <div className="slot-card-secondary">
                  <span className="slot-code-cell">{saved ? <button type="button" className="access-code-copy" aria-label={t('slots.copyAccessCode') + ' — ' + slotName(entry.slot)} onClick={() => void navigator.clipboard.writeText(saved.code)}><code>{saved.code}</code><Icon name="copy" /></button> : <span className="slot-code-unavailable">—</span>}</span>
                  <button type="button" className="primary-action" disabled={Boolean(busy)} onClick={() => void mutateSlot(entry, 'suspend')}>{t('slots.suspend')}</button>
                  <details className="slot-more-actions"><summary aria-label={t('slots.moreActions')}><Icon name="more" /></summary><div className="slot-overflow-menu">
                    <button type="button" className="secondary-action" disabled={Boolean(busy)} onClick={() => void mutateSlot(entry, 'rotate')}>{t('slots.rotateAccessCode')}</button>
                    <button type="button" className="destructive-action" disabled={Boolean(busy)} onClick={() => { if (window.confirm(t('slots.deleteConfirm'))) void mutateSlot(entry, 'delete') }}>{t('slots.delete')}</button>
                    <Link className="nav-link" to={slotHref(entry)}>{t('slots.details')}</Link>
                  </div></details>
                </div>
              </article>
            })}</div>
          </section>
        ))}
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
