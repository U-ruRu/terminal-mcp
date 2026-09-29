import { useCallback, useEffect, useMemo, useState } from 'react'

import { App } from '../App'
import { BrowserConnectionRegistry } from '../connections/registry'
import { ConnectionRuntimeProvider } from '../connections/runtime'
import { browserFleetActorFactory } from './actor'
import { FleetConnectionManager } from './manager'
import { FleetVisibilityController, type FleetVisibilitySource } from './policy'
import { buildFleetReadModel } from './readModel'
import type { FleetInstanceView } from './types'

export type FleetRuntimeDependencies = {
  manager?: FleetConnectionManager
  visibility?: FleetVisibilitySource
}

export function FleetRuntime({ dependencies = {} }: { dependencies?: FleetRuntimeDependencies }) {
  const registry = useMemo(() => new BrowserConnectionRegistry(), [])
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
    const unsubscribe = manager.subscribe(setInstances)
    const visibility = new FleetVisibilityController(
      manager,
      dependencies.visibility ?? document,
    )
    visibility.start()
    void manager.startAll()

    return () => {
      visibility.stop()
      unsubscribe()
      manager.stopAll()
    }
  }, [dependencies.visibility, manager])

  const model = useMemo(() => buildFleetReadModel(instances), [instances])

  const syncProfiles = useCallback(() => {
    setInstances(manager.syncProfiles())
    void manager.startAll()
  }, [manager])

  return (
    <ConnectionRuntimeProvider registry={registry} onProfilesChanged={syncProfiles}>
      <App
      model={model}
      instances={instances}
      loadActivity={(instanceId, options) => manager.activity(instanceId, options)}
      loadTask={(instanceId, namespace, taskId) => manager.task(instanceId, namespace, taskId)}
      />
    </ConnectionRuntimeProvider>
  )
}
