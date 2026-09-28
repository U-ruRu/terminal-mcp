import { Navigate, Route, Routes } from 'react-router-dom'

import type { FleetReadModel } from './fleet/readModel'
import type { FleetInstanceView } from './fleet/types'
import { AppShell } from './components/AppShell'
import { Activity, type ActivityLoader } from './routes/Activity'
import { Overview } from './routes/Overview'
import { Placeholder } from './routes/Placeholder'
import { ServerTasks, type ServerTaskLoader } from './routes/ServerTasks'
import { ServerWorkspace } from './routes/ServerWorkspace'

export type AppProps = {
  model: FleetReadModel
  instances: FleetInstanceView[]
  loadActivity?: ActivityLoader
  loadTask?: ServerTaskLoader
}

export function App({ model, instances, loadActivity, loadTask }: AppProps) {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Overview model={model} />} />
        <Route path="/agents" element={<Placeholder title="Agents" />} />
        <Route path="/tasks" element={<Placeholder title="Tasks" />} />
        <Route path="/context" element={<Placeholder title="Context" />} />
        <Route path="/servers/:instanceId" element={<ServerWorkspace model={model} instances={instances} />} />
        <Route path="/servers/:instanceId/tasks" element={<ServerTasks instances={instances} loadTask={loadTask} />} />
        <Route path="/servers/:instanceId/tasks/:namespace/:taskId" element={<ServerTasks instances={instances} loadTask={loadTask} />} />
        <Route path="/activity" element={<Activity instances={instances} loadActivity={loadActivity} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  )
}
