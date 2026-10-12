import type { ManagedFleetControlReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import type { AccessMeshReadState } from './accessMeshState'

export type SlotScopeContext = { key: string; kind: 'mesh' | 'server' | 'unknown'; label: string; meshId?: string; instanceId: string; memberInstanceIds: string[] }
export function normalizedOrigin(value: string | undefined): string { return (value ?? '').replace(/\/+$/, '').toLowerCase() }

export function contextsFromTopology(instances: FleetInstanceView[], controls: Record<string, ManagedFleetControlReadModel | undefined>, native: Record<string, AccessMeshReadState | undefined>): SlotScopeContext[] {
  const membership = new Map<string, string>(), labels = new Map<string, string>()
  for (const control of Object.values(controls)) {
    if (!control?.managed) continue
    for (const mesh of control.meshes) labels.set(mesh.meshId, mesh.displayName)
    if (control.mesh) labels.set(control.mesh.meshId, control.mesh.displayName)
    for (const node of control.nodes) {
      if (!node.meshId) continue
      const candidate = instances.find((item) => item.profile.instanceId === node.nodeId || normalizedOrigin(item.profile.origin) === normalizedOrigin(node.origin))
      if (candidate) membership.set(candidate.profile.instanceId, node.meshId)
    }
  }
  for (const candidate of instances) {
    const control = controls[candidate.profile.instanceId]
    if (!control?.managed) continue
    const local = control.nodes.find((node) => node.nodeId === control.nodeId || normalizedOrigin(node.origin) === normalizedOrigin(candidate.profile.origin))
    const meshId = local?.meshId ?? control.mesh?.meshId
    if (meshId) membership.set(candidate.profile.instanceId, meshId)
  }
  const contexts = new Map<string, SlotScopeContext>()
  for (const candidate of instances) {
    const id = candidate.profile.instanceId, meshId = membership.get(id), control = controls[id]
    const declaredStandalone = native[id]?.defaults?.writeTarget === 'access_defaults' && native[id]?.defaults?.inheritance?.source === 'standalone'
    const kind = meshId ? 'mesh' : control && !control.managed || declaredStandalone ? 'server' : 'unknown'
    const key = meshId ? `mesh:${meshId}` : kind === 'server' ? `server:${id}` : `unresolved:${id}`
    const existing = contexts.get(key)
    if (existing) {
      existing.memberInstanceIds.push(id)
      const representative = instances.find((item) => item.profile.instanceId === existing.instanceId)
      if (candidate.runtime.authStatus === 'connected' && representative?.runtime.authStatus !== 'connected') existing.instanceId = id
    } else contexts.set(key, { key, kind, label: meshId ? labels.get(meshId) ?? meshId : candidate.profile.displayName, meshId, instanceId: id, memberInstanceIds: [id] })
  }
  return [...contexts.values()].sort((a, b) => a.label.localeCompare(b.label))
}

export function resolveContextKey(key: string, contexts: SlotScopeContext[]): string {
  if (!key.startsWith('unresolved:')) return key
  return contexts.find((item) => item.memberInstanceIds.includes(key.slice('unresolved:'.length)))?.key ?? key
}

export function meshControlForContext(context: SlotScopeContext | undefined, controls: Record<string, ManagedFleetControlReadModel | undefined>): ManagedFleetControlReadModel | undefined {
  if (!context?.meshId) return undefined
  return context.memberInstanceIds.map((id) => controls[id]).filter((control): control is ManagedFleetControlReadModel => Boolean(control?.managed && control.policy))
    .filter((control) => control.mesh?.meshId === context.meshId || control.meshes.some((mesh) => mesh.meshId === context.meshId) || control.nodes.some((node) => node.meshId === context.meshId))
    .sort((a, b) => (b.policy?.revision ?? 0) - (a.policy?.revision ?? 0))[0]
}
