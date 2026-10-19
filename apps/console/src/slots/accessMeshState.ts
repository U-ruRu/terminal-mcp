import { connectionAuthorityScope } from '../connections/authorityScope'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { ConsoleHttpError } from '../api/client'
import type { AccessMeshDefaultsReadModel, AccessMeshMutationRequest, AccessMeshSlotReadModel, AccessMutationResult } from '../api/models'
import { ConsoleOperationError } from '../api/slotContracts'
import type { FleetInstanceView } from '../fleet/types'

export type AccessMeshSlotsLoader = (instanceId: string) => Promise<AccessMeshSlotReadModel[]>
export type AccessMeshDefaultsLoader = (instanceId: string) => Promise<AccessMeshDefaultsReadModel>
export type AccessMeshMutator = (instanceId: string, request: AccessMeshMutationRequest) => Promise<AccessMutationResult>
export type AccessMeshReadTarget = { instance: FleetInstanceView; registryScope: string; available: boolean }
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
type ReadTarget = { id: string; connected: boolean; registryScope: string; authority: string }
type ScopedReadState = { identity: string; state: AccessMeshReadState }

export function slotReadFailure(error: unknown): SlotReadFailure {
  const raw = error instanceof ConsoleHttpError || error instanceof ConsoleOperationError ? error.code : error instanceof Error ? error.message : ''
  const code = /^[a-z][a-z0-9_:-]{0,127}$/.test(raw) ? raw : 'request_failed'
  if (/disabled/.test(code)) return { kind: 'disabled', code }
  if (error instanceof ConsoleHttpError && [404, 405, 501].includes(error.status) || /unsupported|not_implemented/.test(code)) return { kind: 'unsupported', code }
  if (error instanceof ConsoleHttpError && [401, 403].includes(error.status) || /access_denied|permission|forbidden|insufficient_scope|auth_required/.test(code)) return { kind: 'denied', code }
  return { kind: 'failed', code }
}

/** Endpoint reads are independent: a denied defaults read must not erase readable slots. */
export function useAccessMeshState(registries: AccessMeshReadTarget[], loadSlots?: AccessMeshSlotsLoader, loadDefaults?: AccessMeshDefaultsLoader) {
  const sequence = useRef<Record<string, number>>({})
  const alive = useRef(true)
  const inFlight = useRef<Record<string, number | undefined>>({})
  const targets = JSON.stringify(registries.map(({ instance, registryScope, available }) => ({
    id: instance.profile.instanceId, connected: available && instance.runtime.authStatus === 'connected', registryScope,
    // Joining, detaching and re-pairing all invalidate the previous registry read.
    authority: connectionAuthorityScope(instance.profile),
  })).sort((a, b) => a.id.localeCompare(b.id)))
  const entries = useMemo<ReadTarget[]>(() => JSON.parse(targets), [targets])
  const identities = useMemo<Record<string, string>>(() => Object.fromEntries(entries.map((entry) => [entry.id, JSON.stringify(entry)])), [entries])
  const currentIdentities = useRef(identities)
  // Commit the guard before paint or asynchronous callbacks can observe the new scope.
  useLayoutEffect(() => { currentIdentities.current = identities }, [identities])
  const reconciledIdentities = useRef<Record<string, string>>({})

  // Each read belongs to its own target; unrelated registries retain their observations.
  const [cache, setCache] = useState<Record<string, ScopedReadState>>({})
  const [cacheIdentities, setCacheIdentities] = useState(identities)
  if (cacheIdentities !== identities) {
    setCacheIdentities(identities)
    setCache((current) => Object.fromEntries(Object.entries(current).filter(([id, entry]) => entry.identity === identities[id])))
  }

  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const refresh = useCallback(async (instanceId: string): Promise<AccessMeshReadState | undefined> => {
    const identity = identities[instanceId]
    if (!identity || !alive.current || currentIdentities.current[instanceId] !== identity) return undefined
    if (!loadSlots || !loadDefaults || !entries.some((item) => item.id === instanceId && item.connected)) return undefined
    const serial = (sequence.current[instanceId] ?? 0) + 1
    sequence.current[instanceId] = serial
    inFlight.current[instanceId] = serial
    const isCurrent = () => alive.current && currentIdentities.current[instanceId] === identity && sequence.current[instanceId] === serial
    const publish = (patch: Partial<AccessMeshReadState>) => {
      if (!isCurrent()) return
      setCache((current) => {
        if (!isCurrent()) return current
        const previous = current[instanceId]?.identity === identity ? current[instanceId].state : undefined
        return { ...current, [instanceId]: { identity, state: { observedAt: 0, ...previous, ...patch } } }
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
    if (!isCurrent()) return undefined
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
  }, [entries, identities, loadDefaults, loadSlots])

  const removeSlot = useCallback((registryScope: string, issuerNodeId: string, slotId: string) => {
    if (!alive.current) return
    const ids = new Set(entries.filter((item) => item.registryScope === registryScope && currentIdentities.current[item.id] === identities[item.id]).map((item) => item.id))
    if (!ids.size) return
    // Reads started before the confirmed deletion must not restore its old row.
    for (const instanceId of ids) {
      sequence.current[instanceId] = (sequence.current[instanceId] ?? 0) + 1
      delete inFlight.current[instanceId]
    }
    setCache((current) => Object.fromEntries(Object.entries(current).map(([id, entry]) => [id,
      ids.has(id) && currentIdentities.current[id] === identities[id] && entry.identity === identities[id]
        ? { ...entry, state: { ...entry.state,
          slots: entry.state.slots?.filter((slot) => slot.issuerNodeId !== issuerNodeId || slot.slotId !== slotId),
          slotsPending: false, defaultsPending: false,
        } }
        : entry,
    ])))
  }, [entries, identities])

  useEffect(() => {
    const previous = reconciledIdentities.current
    for (const [id, identity] of Object.entries(previous)) {
      if (identities[id] === identity) continue
      sequence.current[id] = (sequence.current[id] ?? 0) + 1
      delete inFlight.current[id]
    }
    reconciledIdentities.current = loadSlots && loadDefaults ? identities : {}
    for (const entry of entries) if (entry.connected && previous[entry.id] !== identities[entry.id]) void refresh(entry.id)
  }, [entries, identities, loadDefaults, loadSlots, refresh])

  const currentRead = useRef({ entries, refresh })
  useLayoutEffect(() => { currentRead.current = { entries, refresh } }, [entries, refresh])
  useEffect(() => {
    const refreshIdle = () => {
      if (document.visibilityState === 'hidden') return
      const { entries, refresh } = currentRead.current
      for (const entry of entries) if (entry.connected && !inFlight.current[entry.id]) void refresh(entry.id)
    }
    const timer = window.setInterval(refreshIdle, 15_000)
    document.addEventListener('visibilitychange', refreshIdle)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshIdle)
    }
  }, [])

  const states = useMemo<Record<string, AccessMeshReadState | undefined>>(() => Object.fromEntries(
    Object.entries(cache).filter(([id, entry]) => entry.identity === identities[id]).map(([id, entry]) => [id, entry.state]),
  ), [cache, identities])
  return { states, refresh, removeSlot }
}
