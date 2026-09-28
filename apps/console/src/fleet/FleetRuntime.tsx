import { useEffect, useMemo, useState } from 'react'

import { App } from '../App'
import { BrowserConnectionRegistry } from '../connections/registry'
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

  return (
    <App
      model={model}
      instances={instances}
      loadActivity={(instanceId, options) => manager.activity(instanceId, options)}
      loadTask={(instanceId, namespace, taskId) => manager.task(instanceId, namespace, taskId)}
    />
  )
}
