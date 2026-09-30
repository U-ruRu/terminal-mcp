import { App as CapacitorApp } from '@capacitor/app'
import { Capacitor } from '@capacitor/core'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { App } from '../App'
import { PairingHandoffController } from '../connections/PairingHandoffController'
import { BrowserConnectionRegistry } from '../connections/registry'
import { ConnectionRuntimeProvider } from '../connections/runtime'
import { BrowserDiagnosticJournal, diagnosticErrorCode } from '../diagnostics/journal'
import { browserFleetActorFactory } from './actor'
import { FleetConnectionManager } from './manager'
import { defaultFleetVisibilitySource } from './nativeVisibility'
import { FleetVisibilityController, type FleetVisibilitySource } from './policy'
import { buildFleetReadModel } from './readModel'
import type { FleetInstanceView } from './types'

export type FleetRuntimeDependencies = {
  manager?: FleetConnectionManager
  visibility?: FleetVisibilitySource
}

export function FleetRuntime({ dependencies = {} }: { dependencies?: FleetRuntimeDependencies }) {
  const registry = useMemo(() => new BrowserConnectionRegistry(), [])
  const diagnostics = useMemo(() => new BrowserDiagnosticJournal(), [])
  const runtimeSignatures = useRef(new Map<string, string>())
  const bootRecorded = useRef(false)
  const manager = useMemo(
    () =>
      dependencies.manager ??
      new FleetConnectionManager(
        new BrowserConnectionRegistry(),
        browserFleetActorFactory(),
      ),
    [dependencies.manager],
  )

  const [instances, setInstances] = useState<FleetInstanceView[]>(() => manager.syncProfiles())

  useEffect(() => {
    if (!bootRecorded.current) {
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
    }

    const observeInstances = (next: FleetInstanceView[]) => {
      setInstances(next)
      const present = new Set<string>()
      for (const item of next) {
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
        const details = [
          `attempt=${item.runtime.reconnectAttempt}`,
          realtime?.staleReason ? `stale=${realtime.staleReason}` : '',
          realtime?.socketConnected ? 'socket=open' : 'socket=closed',
          realtime?.snapshot ? 'snapshot=available' : 'snapshot=missing',
          item.runtime.lastError ? `error=${item.runtime.lastError}` : '',
        ].filter(Boolean).join(' ')
        diagnostics.append({
          type: item.runtime.status === 'live' ? 'connection_live' : 'connection_state',
          instanceId: item.profile.instanceId,
          server: item.profile.displayName,
          status: item.runtime.status,
          authStatus: item.runtime.authStatus,
          code: diagnosticErrorCode(item.runtime.lastError ?? realtime?.staleReason),
          detail: details,
        })
      }
      for (const instanceId of runtimeSignatures.current.keys()) {
        if (!present.has(instanceId)) runtimeSignatures.current.delete(instanceId)
      }
    }

    const unsubscribe = manager.subscribe(observeInstances)
    const visibility = new FleetVisibilityController(
      manager,
      dependencies.visibility ?? defaultFleetVisibilitySource(),
      undefined,
      undefined,
      (event) => diagnostics.append({ type: event }),
    )
    visibility.start()
    void manager.startAll()

    return () => {
      visibility.stop()
      unsubscribe()
      manager.stopAll()
    }
  }, [dependencies.visibility, diagnostics, manager])

  const model = useMemo(() => buildFleetReadModel(instances), [instances])

  const syncProfiles = useCallback(() => {
    setInstances(manager.syncProfiles())
    void manager.startAll()
  }, [manager])

  return (
    <ConnectionRuntimeProvider registry={registry} diagnostics={diagnostics} onProfilesChanged={syncProfiles} restoreOnMount={false}>
      <PairingHandoffController />
      <App
      model={model}
      diagnostics={diagnostics}
      instances={instances}
      loadActivity={(instanceId, options) => manager.activity(instanceId, options)}
      loadTask={(instanceId, namespace, taskId) => manager.task(instanceId, namespace, taskId)}
      mutatePersistent={(instanceId, path, body) => manager.persistentMutation(instanceId, path, body)}
      />
    </ConnectionRuntimeProvider>
  )
}
