import type { FleetInstanceView } from './types'

export function preserveSavedServerInstances(
  projected: FleetInstanceView[],
  direct: FleetInstanceView[],
): FleetInstanceView[] {
  const merged = [...projected]
  const present = new Set(projected.map((item) => item.profile.instanceId))
  for (const item of direct) {
    if (!present.has(item.profile.instanceId)) merged.push(item)
  }
  return merged
}
