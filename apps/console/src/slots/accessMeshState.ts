import { useCallback, useEffect, useRef, useState } from 'react'

import { ConsoleHttpError } from '../api/client'
import type { AccessMeshDefaultsReadModel, AccessMeshMutationRequest, AccessMeshSlotReadModel, PersistentMutationResult } from '../api/models'
import { ConsoleOperationError } from '../api/slotContracts'
import type { FleetInstanceView } from '../fleet/types'

export type AccessMeshSlotsLoader = (instanceId: string) => Promise<AccessMeshSlotReadModel[]>
export type AccessMeshDefaultsLoader = (instanceId: string) => Promise<AccessMeshDefaultsReadModel>
export type AccessMeshMutator = (instanceId: string, request: AccessMeshMutationRequest) => Promise<PersistentMutationResult>
export type SlotReadFailure = { kind: 'unsupported' | 'disabled' | 'denied' | 'failed'; code: string }
export type AccessMeshReadState = {
  slots?: AccessMeshSlotReadModel[]
  defaults?: AccessMeshDefaultsReadModel
  slotsError?: SlotReadFailure
  defaultsError?: SlotReadFailure
  observedAt: number
}

export function slotReadFailure(error: unknown): SlotReadFailure {
  const raw = error instanceof ConsoleHttpError || error instanceof ConsoleOperationError ? error.code : error instanceof Error ? error.message : ''
  const code = /^[a-z][a-z0-9_:-]{0,127}$/.test(raw) ? raw : 'request_failed'
  if (/disabled/.test(code)) return { kind: 'disabled', code }
  if (error instanceof ConsoleHttpError && [404, 405, 501].includes(error.status) || /unsupported|not_implemented/.test(code)) return { kind: 'unsupported', code }
  if (error instanceof ConsoleHttpError && [401, 403].includes(error.status) || /access_denied|permission|forbidden|insufficient_scope|auth_required/.test(code)) return { kind: 'denied', code }
  return { kind: 'failed', code }
}

/** Endpoint reads are independent: a denied defaults read must not erase readable slots. */
export function useAccessMeshState(instances: FleetInstanceView[], loadSlots?: AccessMeshSlotsLoader, loadDefaults?: AccessMeshDefaultsLoader) {
  const sequence = useRef<Record<string, number>>({})
  const alive = useRef(true)
  const targets = JSON.stringify(instances.map((instance) => ({
    id: instance.profile.instanceId, connected: instance.runtime.authStatus === 'connected',
    // Re-pairing is a new authority read even if the human-facing server name is unchanged.
    origin: instance.profile.origin, credential: instance.profile.credentialRef,
    device: instance.profile.metadata.deviceId, client: instance.profile.metadata.clientId,
  })).sort((a, b) => a.id.localeCompare(b.id)))

  // An authority change invalidates its previous read before any action can render.
  const [cache, setCache] = useState<{ targets: string; states: Record<string, AccessMeshReadState | undefined> }>({ targets, states: {} })
  if (cache.targets !== targets) setCache({ targets, states: {} })

  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const refresh = useCallback(async (instanceId: string): Promise<AccessMeshReadState | undefined> => {
    const targetList: Array<{ id: string; connected: boolean }> = JSON.parse(targets)
    if (!loadSlots || !loadDefaults || !targetList.some((item) => item.id === instanceId && item.connected)) return undefined
    const serial = (sequence.current[instanceId] ?? 0) + 1
    sequence.current[instanceId] = serial
    const [slots, defaults] = await Promise.allSettled([loadSlots(instanceId), loadDefaults(instanceId)])
    const result: AccessMeshReadState = {
      slots: slots.status === 'fulfilled' ? slots.value : undefined,
      defaults: defaults.status === 'fulfilled' ? defaults.value : undefined,
      slotsError: slots.status === 'rejected' ? slotReadFailure(slots.reason) : undefined,
      defaultsError: defaults.status === 'rejected' ? slotReadFailure(defaults.reason) : undefined,
      observedAt: Date.now(),
    }
    if (!alive.current || sequence.current[instanceId] !== serial) return undefined
    setCache((current) => {
      if (current.targets !== targets) return current
      return { ...current, states: { ...current.states, [instanceId]: {
        ...result,
        slots: result.slots ?? current.states[instanceId]?.slots,
        defaults: result.defaults ?? current.states[instanceId]?.defaults,
      } } }
    })
    return result
  }, [loadDefaults, loadSlots, targets])

  useEffect(() => {
    const entries: Array<{ id: string; connected: boolean }> = JSON.parse(targets)
    for (const entry of entries) if (entry.connected) void refresh(entry.id)
    const serials = sequence.current
    return () => {
      for (const entry of entries) serials[entry.id] = (serials[entry.id] ?? 0) + 1
    }
  }, [refresh, targets])

  return { states: cache.targets === targets ? cache.states : {}, refresh }
}
