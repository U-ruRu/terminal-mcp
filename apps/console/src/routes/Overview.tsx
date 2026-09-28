import { FleetDashboard } from '../components/FleetDashboard'
import type { FleetReadModel } from '../fleet/readModel'

export function Overview({ model }: { model: FleetReadModel }) {
  return <FleetDashboard model={model} />
}
