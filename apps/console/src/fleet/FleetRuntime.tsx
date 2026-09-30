import { App as CapacitorApp } from '@capacitor/app'
import { Capacitor } from '@capacitor/core'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { App } from '../App'
import { PairingHandoffController } from '../connections/PairingHandoffController'
import { BrowserConnectionRegistry } from '../connections/registry'
import { ConnectionRuntimeProvider } from '../connections/runtime'
import { BrowserDiagnosticJournal, diagnosticErrorCode } from '../diagnostics/journal'
import { FleetAdaptiveReadRuntime, type FleetAdaptiveRuntimeState } from './adaptiveRuntime'
import { FleetAdaptiveLifecycle } from './adaptiveLifecycle'
import { browserFleetActorFactory } from './actor'
import { browserFleetIngressEndpoints, type BrowserFleetIngressEndpoint } from './browserIngress'
import { BrowserFleetProjectionCache } from './cache'
import { BrowserDirectAuthorityClient } from './directAuthority'
import { fleetV1ClientEnabled } from './flags'
import { FleetConnectionManager } from './manager'
import { defaultFleetVisibilitySource } from './nativeVisibility'
import { FleetVisibilityController, type FleetVisibilitySource } from './policy'
import { buildProjectedFleetInstances, projectedActivity, projectedTask, type FleetSourceBinding } from './projectionAdapter'
import { buildFleetReadModel } from './readModel'
import type { FleetInstanceView } from './types'

export type FleetRuntimeDependencies = {
  manager?: FleetConnectionManager
  visibility?: FleetVisibilitySource
  fleetV1Enabled?: boolean
}

async function discoverBindings(
  endpoints: BrowserFleetIngressEndpoint[],
  fleetId: string,
): Promise<FleetSourceBinding[]> {
  const values = await Promise.all(endpoints.map(async (endpoint) => {
    try {
      if (!endpoint.nodeId) await endpoint.probe()
      if (!endpoint.nodeId || endpoint.fleetId !== fleetId) return null
      return { sourceNodeId: endpoint.nodeId, profile: endpoint.profile }
    } catch {
      return null
    }
  }))
  return values.filter((item): item is FleetSourceBinding => item !== null)
}

export function FleetRuntime({ dependencies = {} }: { dependencies?: FleetRuntimeDependencies }) {
  const registry = useMemo(() => new BrowserConnectionRegistry(), [])
  const diagnostics = useMemo(() => new BrowserDiagnosticJournal(), [])
  const fleetEnabled = useMemo(
    () => dependencies.fleetV1Enabled ?? fleetV1ClientEnabled(),
    [dependencies.fleetV1Enabled],
  )
  const manager = useMemo(
    () => dependencies.manager ?? new FleetConnectionManager(registry, browserFleetActorFactory()),
    [dependencies.manager, registry],
  )
  const directAuthority = useMemo(() => new BrowserDirectAuthorityClient(registry), [registry])
  const cache = useMemo(
    () => fleetEnabled ? new BrowserFleetProjectionCache() : null,
    [fleetEnabled],
  )
  const fleetRuntimeRef = useRef<FleetAdaptiveReadRuntime | null>(null)
  const runtimeSignatures = useRef(new Map<string, string>())
  const fleetSignature = useRef('')
  const bootRecorded = useRef(false)

  const [directInstances, setDirectInstances] = useState<FleetInstanceView[]>(
    () => manager.syncProfiles(),
  )
  const [adaptiveState, setAdaptiveState] = useState<FleetAdaptiveRuntimeState | null>(null)
  const [bindings, setBindings] = useState<FleetSourceBinding[]>([])
  const [profileRevision, setProfileRevision] = useState(0)

  useEffect(() => {
    if (bootRecorded.current) return
    bootRecorded.current = true
    void (async () => {
      if (Capacitor.isNativePlatform()) {
        try {
          const info = await CapacitorApp.getInfo()
          diagnostics.recordApplicationStart(info.version, info.build)
          return
        } catch {
          diagnostics.append({ type: 'app_started', detail: 'native version unavailable' })
          return
        }
      }
      diagnostics.recordApplicationStart('web', 'web')
    })()
  }, [diagnostics])

  useEffect(() => manager.subscribe(setDirectInstances), [manager])

  useEffect(() => {
    const visibilitySource = dependencies.visibility ?? defaultFleetVisibilitySource()

    if (!fleetEnabled || !cache) {
      fleetRuntimeRef.current = null
      const visibility = new FleetVisibilityController(
        manager,
        visibilitySource,
        undefined,
        undefined,
        (event) => diagnostics.append({ type: event }),
      )
      visibility.start()
      void manager.startAll()
      return () => {
        visibility.stop()
        manager.stopAll()
      }
    }

    let cancelled = false
    let bindingFleetId = ''
    manager.stopAll()

    const endpoints = browserFleetIngressEndpoints(registry.list(), registry)
    const runtime = new FleetAdaptiveReadRuntime(cache)
    fleetRuntimeRef.current = runtime

    const unsubscribe = runtime.subscribe((state) => {
      if (cancelled) return
      setAdaptiveState(state)

      if (state.status === 'fallback') void manager.startAll()
      else if (state.status === 'live') manager.stopAll()

      if (state.fleetId && bindingFleetId !== state.fleetId) {
        bindingFleetId = state.fleetId
        void discoverBindings(endpoints, state.fleetId).then((next) => {
          if (!cancelled) setBindings(next)
        })
      }

      const signature = [
        state.status,
        state.selectorState,
        state.activeIngressId ?? '',
        state.preferredIngressId ?? '',
        state.projectionEpoch,
        state.networkEpoch,
        state.lastSwitchReason ?? '',
        state.lastError ?? '',
      ].join('|')
      if (fleetSignature.current !== signature) {
        fleetSignature.current = signature
        diagnostics.append({
          type: 'fleet_v1_state',
          status: state.status,
          code: diagnosticErrorCode(state.lastError),
          detail: [
            `selector=${state.selectorState}`,
            `ingress=${state.activeIngressId ?? 'none'}`,
            `preferred=${state.preferredIngressId ?? 'none'}`,
            `epoch=${state.projectionEpoch}`,
            `seq=${state.projectionSeq}`,
            `network_epoch=${state.networkEpoch}`,
            state.lastSwitchReason ? `switch=${state.lastSwitchReason}` : '',
          ].filter(Boolean).join(' '),
        })
      }
    })

    const lifecycle = new FleetAdaptiveLifecycle(
      runtime,
      () => endpoints,
      undefined,
      (error) => diagnostics.append({
        type: 'fleet_v1_error',
        code: diagnosticErrorCode(
          error instanceof Error ? error.message : 'fleet_runtime_error',
        ),
      }),
    )
    const visibility = new FleetVisibilityController(
      lifecycle,
      visibilitySource,
      undefined,
      undefined,
      (event) => diagnostics.append({ type: event }),
    )
    visibility.start()

    const onNetwork = () => {
      runtime.networkChanged()
      void lifecycle.recoverAll()
    }
    window.addEventListener('online', onNetwork)
    window.addEventListener('offline', onNetwork)
    void lifecycle.startAll()

    return () => {
      cancelled = true
      window.removeEventListener('online', onNetwork)
      window.removeEventListener('offline', onNetwork)
      visibility.stop()
      lifecycle.stopAll()
      unsubscribe()
      fleetRuntimeRef.current = null
      manager.stopAll()
    }
  }, [cache, dependencies.visibility, diagnostics, fleetEnabled, manager, profileRevision, registry])

  const projectedInstances = useMemo(
    () => adaptiveState?.cache.fleetId
      ? buildProjectedFleetInstances(adaptiveState.cache, bindings, adaptiveState.status)
      : [],
    [adaptiveState, bindings],
  )
  const directHasSnapshot = directInstances.some((item) => item.runtime.realtime?.snapshot)
  const useProjected = Boolean(
    fleetEnabled &&
    adaptiveState?.cache.fleetId &&
    projectedInstances.length > 0 &&
    (adaptiveState.status !== 'fallback' || !directHasSnapshot),
  )
  const instances = useProjected ? projectedInstances : directInstances

  useEffect(() => {
    const present = new Set<string>()
    for (const item of instances) {
      present.add(item.profile.instanceId)
      const realtime = item.runtime.realtime
      const signature = [
        item.runtime.status,
        item.runtime.authStatus,
        item.runtime.reconnectAttempt,
        item.runtime.lastError ?? '',
        realtime?.staleReason ?? '',
        realtime?.socketConnected ? 'socket' : 'nosocket',
        realtime?.snapshot ? 'snapshot' : 'nosnapshot',
      ].join('|')
      if (runtimeSignatures.current.get(item.profile.instanceId) === signature) continue
      runtimeSignatures.current.set(item.profile.instanceId, signature)
      diagnostics.append({
        type: item.runtime.status === 'live' ? 'connection_live' : 'connection_state',
        instanceId: item.profile.instanceId,
        server: item.profile.displayName,
        status: item.runtime.status,
        authStatus: item.runtime.authStatus,
        code: diagnosticErrorCode(item.runtime.lastError ?? realtime?.staleReason),
        detail: [
          `attempt=${item.runtime.reconnectAttempt}`,
          realtime?.staleReason ? `stale=${realtime.staleReason}` : '',
          realtime?.socketConnected ? 'socket=open' : 'socket=closed',
          realtime?.snapshot ? 'snapshot=available' : 'snapshot=missing',
        ].filter(Boolean).join(' '),
      })
    }
    for (const instanceId of runtimeSignatures.current.keys()) {
      if (!present.has(instanceId)) runtimeSignatures.current.delete(instanceId)
    }
  }, [diagnostics, instances])

  const model = useMemo(() => buildFleetReadModel(instances), [instances])

  const syncProfiles = useCallback(() => {
    setDirectInstances(manager.syncProfiles())
    setProfileRevision((value) => value + 1)
    if (!fleetEnabled) void manager.startAll()
  }, [fleetEnabled, manager])

  const sourceForProfile = useMemo(
    () => new Map(bindings.map((item) => [item.profile.instanceId, item.sourceNodeId])),
    [bindings],
  )

  return (
    <ConnectionRuntimeProvider
      registry={registry}
      diagnostics={diagnostics}
      onProfilesChanged={syncProfiles}
      restoreOnMount={false}
    >
      <PairingHandoffController />
      <App
        model={model}
        diagnostics={diagnostics}
        instances={instances}
        loadActivity={(instanceId, options) => {
          if (useProjected && adaptiveState) {
            const source = sourceForProfile.get(instanceId)
            if (source) return Promise.resolve(projectedActivity(adaptiveState.cache, source, options))
          }
          return directAuthority.activity(instanceId, options)
        }}
        loadTask={(instanceId, namespace, taskId) => {
          if (useProjected && adaptiveState) {
            const source = sourceForProfile.get(instanceId)
            const task = source
              ? projectedTask(adaptiveState.cache, source, namespace, taskId)
              : undefined
            if (task) return Promise.resolve(task)
            return Promise.reject(new Error('fleet_task_not_cached'))
          }
          return directAuthority.task(instanceId, namespace, taskId)
        }}
        mutatePersistent={async (instanceId, path, body) => {
          const result = await directAuthority.persistentMutation(instanceId, path, body)
          if (result.ok && fleetRuntimeRef.current) void fleetRuntimeRef.current.syncOnce()
          return result
        }}
      />
    </ConnectionRuntimeProvider>
  )
}
