import { Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from './components/AppShell'
import type { BrowserDiagnosticJournal } from './diagnostics/journal'
import type { FleetReadModel } from './fleet/readModel'
import type { FleetInstanceView } from './fleet/types'
import { Activity, type ActivityLoader } from './routes/Activity'
import { Connections } from './routes/Connections'
import { Overview } from './routes/Overview'
import { ServerAgents } from './routes/ServerAgents'
import { ServerChooser } from './routes/ServerChooser'
import { Settings } from './routes/Settings'
import { ServerSection } from './routes/ServerSection'
import { ServerTasks, type ServerTaskLoader } from './routes/ServerTasks'
import { ServerWorkspace } from './routes/ServerWorkspace'

export type AppProps = {
  model: FleetReadModel
  instances: FleetInstanceView[]
  loadActivity?: ActivityLoader
  loadTask?: ServerTaskLoader
  diagnostics?: BrowserDiagnosticJournal
}

export function App({ model, instances, loadActivity, loadTask, diagnostics }: AppProps) {
  return (
    <AppShell servers={model.servers}>
      <Routes>
        <Route path="/" element={<Overview model={model} />} />
        <Route path="/servers" element={<Overview model={model} />} />
        <Route path="/agents" element={<ServerChooser model={model} section="agents" />} />
        <Route path="/tasks" element={<ServerChooser model={model} section="tasks" />} />
        <Route path="/context" element={<ServerChooser model={model} section="context" />} />
        <Route path="/health" element={<ServerChooser model={model} section="health" />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/connect" element={<Connections />} />
        <Route path="/servers/:instanceId" element={<ServerWorkspace model={model} instances={instances} />} />
        <Route path="/servers/:instanceId/agents" element={<ServerAgents instances={instances} />} />
        <Route path="/servers/:instanceId/agents/:agentId" element={<ServerAgents instances={instances} />} />
        <Route path="/servers/:instanceId/context" element={<ServerSection model={model} section="context" diagnostics={diagnostics} />} />
        <Route path="/servers/:instanceId/health" element={<ServerSection model={model} section="health" diagnostics={diagnostics} />} />
        <Route path="/servers/:instanceId/tasks" element={<ServerTasks instances={instances} loadTask={loadTask} />} />
        <Route path="/servers/:instanceId/tasks/:namespace/:taskId" element={<ServerTasks instances={instances} loadTask={loadTask} />} />
        <Route path="/activity" element={<Activity instances={instances} loadActivity={loadActivity} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  )
}
