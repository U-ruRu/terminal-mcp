import { connectionAuthorityScope } from '../connections/authorityScope'
import { useCallback, useEffect, useRef, useState } from 'react'

import { ConsoleHttpError } from '../api/client'
import type { AccessMeshDefaultsReadModel, AccessMeshMutationRequest, AccessMeshSlotReadModel, AccessMutationResult } from '../api/models'
import { ConsoleOperationError } from '../api/slotContracts'
import type { FleetInstanceView } from '../fleet/types'

export type AccessMeshSlotsLoader = (instanceId: string) => Promise<AccessMeshSlotReadModel[]>
export type AccessMeshDefaultsLoader = (instanceId: string) => Promise<AccessMeshDefaultsReadModel>
export type AccessMeshMutator = (instanceId: string, request: AccessMeshMutationRequest) => Promise<AccessMutationResult>
export type SlotReadFailure = { kind: 'unsupported' | 'disabled' | 'denied' | 'failed'; code: string }
export type AccessMeshReadState = {
  slots?: AccessMeshSlotReadModel[]
  defaults?: AccessMeshDefaultsReadModel
  slotsError?: SlotReadFailure
  defaultsError?: SlotReadFailure
  observedAt: number
  defaultsObservedAt?: number
  slotsPending?: boolean
  defaultsPending?: boolean
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
  const inFlight = useRef<Record<string, number | undefined>>({})
  const targets = JSON.stringify(instances.map((instance) => ({
    id: instance.profile.instanceId, connected: instance.runtime.authStatus === 'connected',
    // Re-pairing is a new authority read even if the human-facing server name is unchanged.
    authority: connectionAuthorityScope(instance.profile),
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
    inFlight.current[instanceId] = serial
    const publish = (patch: Partial<AccessMeshReadState>) => {
      if (!alive.current || sequence.current[instanceId] !== serial) return
      setCache((current) => current.targets !== targets ? current : {
        ...current, states: { ...current.states, [instanceId]: {
          observedAt: 0, ...current.states[instanceId], ...patch,
        } },
      })
    }
    publish({ slotsPending: true, defaultsPending: true })
    // Independent settlement: a slow defaults endpoint must not hide readable slots.
    const slotsRequest = Promise.resolve().then(() => loadSlots(instanceId)).then((value) => {
      const observedAt = Date.now()
      publish({ slots: value, slotsError: undefined, slotsPending: false, observedAt })
      return { value, observedAt }
    }, (error: unknown) => {
      // Retained rows keep their original sample time; a failed refresh is not a new observation.
      publish({ slotsError: slotReadFailure(error), slotsPending: false })
      throw error
    })
    const defaultsRequest = Promise.resolve().then(() => loadDefaults(instanceId)).then((value) => {
      const observedAt = Date.now()
      publish({ defaults: value, defaultsError: undefined, defaultsPending: false, defaultsObservedAt: observedAt })
      return { value, observedAt }
    }, (error: unknown) => {
      publish({ defaultsError: slotReadFailure(error), defaultsPending: false })
      throw error
    })
    const [slots, defaults] = await Promise.allSettled([slotsRequest, defaultsRequest])
    if (inFlight.current[instanceId] === serial) delete inFlight.current[instanceId]
    if (!alive.current || sequence.current[instanceId] !== serial) return undefined
    const result: AccessMeshReadState = {
      slots: slots.status === 'fulfilled' ? slots.value.value : undefined,
      defaults: defaults.status === 'fulfilled' ? defaults.value.value : undefined,
      slotsError: slots.status === 'rejected' ? slotReadFailure(slots.reason) : undefined,
      defaultsError: defaults.status === 'rejected' ? slotReadFailure(defaults.reason) : undefined,
      observedAt: slots.status === 'fulfilled' ? slots.value.observedAt : 0,
      defaultsObservedAt: defaults.status === 'fulfilled' ? defaults.value.observedAt : undefined,
      slotsPending: false, defaultsPending: false,
    }
    return result
  }, [loadDefaults, loadSlots, targets])

  useEffect(() => {
    const entries: Array<{ id: string; connected: boolean }> = JSON.parse(targets)
    for (const entry of entries) if (entry.connected) void refresh(entry.id)
    const refreshIdle = () => {
      if (document.visibilityState === 'hidden') return
      for (const entry of entries) if (entry.connected && !inFlight.current[entry.id]) void refresh(entry.id)
    }
    const timer = window.setInterval(refreshIdle, 15_000)
    document.addEventListener('visibilitychange', refreshIdle)
    const serials = sequence.current
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshIdle)
      for (const entry of entries) serials[entry.id] = (serials[entry.id] ?? 0) + 1
    }
  }, [refresh, targets])

  return { states: cache.targets === targets ? cache.states : {}, refresh }
}
