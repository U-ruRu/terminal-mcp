import type { ManagedFleetControlReadModel } from '../api/models'
import type { FleetInstanceView } from '../fleet/types'
import { observedMembership } from '../connections/membershipReadiness'

export type SlotScopeContext = {
  key: string
  kind: 'mesh' | 'server' | 'unknown'
  label: string
  meshId?: string
  authorityNodeId?: string
  registryScope: string
  instanceId: string
  memberInstanceIds: string[]
  available: boolean
}

/** Only the participant's own observation identifies the registry it currently serves. */
export function contextsFromTopology(
  instances: FleetInstanceView[],
  controls: Record<string, ManagedFleetControlReadModel | undefined>,
  available: Record<string, boolean | undefined>,
): SlotScopeContext[] {
  const contexts = new Map<string, SlotScopeContext>()
  const ranked = [...instances].sort((a, b) => {
    const rank = (item: FleetInstanceView) => {
      const id = item.profile.instanceId, control = controls[id]
      return Number(Boolean(available[id])) * 4
        + Number(item.profile.metadata.scope.split(/\s+/).includes('terminal:execute')) * 2
        + Number(Boolean(control && control.nodeId === control.controlNodeId))
    }
    return rank(b) - rank(a) || a.profile.instanceId.localeCompare(b.profile.instanceId)
  })
  for (const candidate of ranked) {
    const id = candidate.profile.instanceId, control = controls[id]
    const membership = observedMembership({ control, freshness: available[id] ? 'fresh' : 'unknown' })
    const meshId = membership.kind === 'mesh' ? membership.meshId : undefined
    const kind = meshId ? 'mesh' : membership.kind === 'standalone' ? 'server' : 'unknown'
    const key = meshId ? `mesh:${meshId}` : kind === 'server' ? `server:${id}` : `unresolved:${id}`
    const existing = contexts.get(key)
    if (existing) {
      existing.memberInstanceIds.push(id)
      continue
    }
    const authorityNodeId = control?.controlNodeId
    const mesh = control?.meshes.find((item) => item.meshId === meshId) ?? control?.mesh
    contexts.set(key, {
      key, kind, meshId, authorityNodeId,
      registryScope: kind === 'unknown' ? '' : JSON.stringify([key, authorityNodeId]),
      label: meshId ? mesh?.displayName ?? meshId : candidate.profile.displayName,
      instanceId: id, memberInstanceIds: [id], available: Boolean(available[id]) && kind !== 'unknown',
    })
  }
  return [...contexts.values()].sort((a, b) => a.label.localeCompare(b.label))
}

export function resolveContextKey(key: string, contexts: SlotScopeContext[]): string {
  if (!key.startsWith('unresolved:')) return key
  return contexts.find((item) => item.memberInstanceIds.includes(key.slice('unresolved:'.length)))?.key ?? key
}

export function meshControlForContext(context: SlotScopeContext | undefined, controls: Record<string, ManagedFleetControlReadModel | undefined>): ManagedFleetControlReadModel | undefined {
  if (!context?.meshId) return undefined
  const control = controls[context.instanceId]
  const membership = observedMembership({ control, freshness: 'unknown' })
  return control?.policy && membership.meshId === context.meshId && control.controlNodeId === context.authorityNodeId ? control : undefined
}
