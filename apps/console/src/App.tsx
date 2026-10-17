import { connectionAuthorityScope } from './connections/authorityScope'
import { Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from './components/AppShell'
import type { BrowserDiagnosticJournal } from './diagnostics/journal'
import type { FleetReadModel } from './fleet/readModel'
import type { FleetInstanceView } from './fleet/types'
import type { AccessMeshDefaultsReadModel, AccessMeshMutationRequest, AccessMeshSlotReadModel, ContextReadModel, ManagedFleetControlReadModel, ManagedFleetMutationResult, AccessMutationResult } from './api/models'
import { Activity, type ActivityLoader } from './routes/Activity'
import { Connections } from './routes/Connections'
import { Overview } from './routes/Overview'
import { ServerAgents } from './routes/ServerAgents'
import { ServerChooser } from './routes/ServerChooser'
import { Settings } from './routes/Settings'
import { ServerSection } from './routes/ServerSection'
import { ServerSlots } from './routes/ServerSlots'
import { ServerTasks, type ServerTaskLoader } from './routes/ServerTasks'
import { ServerWorkspace } from './routes/ServerWorkspace'
import { UiKitShowcase } from './routes/UiKitShowcase'

export type AppProps = {
  model: FleetReadModel
  instances: FleetInstanceView[]
  loadActivity?: ActivityLoader
  loadTask?: ServerTaskLoader
  loadContexts?: (instanceId: string) => Promise<ContextReadModel[]>
  loadAccessMeshSlots?: (instanceId: string) => Promise<AccessMeshSlotReadModel[]>
  loadAccessMeshDefaults?: (instanceId: string) => Promise<AccessMeshDefaultsReadModel>
  mutateAccessMesh?: (instanceId: string, body: AccessMeshMutationRequest) => Promise<AccessMutationResult>
  loadFleetControl?: (instanceId: string) => Promise<ManagedFleetControlReadModel>
  mutateFleetControl?: (instanceId: string, path: string, body: Record<string, unknown>) => Promise<ManagedFleetMutationResult>
  diagnostics?: BrowserDiagnosticJournal
}

export function App({ model, instances, loadActivity, loadTask, loadContexts, loadAccessMeshSlots, loadAccessMeshDefaults, mutateAccessMesh, loadFleetControl, mutateFleetControl, diagnostics }: AppProps) {
  const connectionGeneration = JSON.stringify(instances.map((item) => connectionAuthorityScope(item.profile)))
  const slotApi = { loadAccessMeshSlots, loadAccessMeshDefaults, mutateAccessMesh }
  return (
    <AppShell servers={model.servers}>
      <Routes>
        <Route path="/" element={<Overview key={connectionGeneration} {...slotApi} model={model} instances={instances} loadFleetControl={loadFleetControl} />} />
        <Route path="/servers" element={<Overview key={connectionGeneration} {...slotApi} model={model} instances={instances} loadFleetControl={loadFleetControl} />} />
        <Route path="/agents" element={<ServerChooser model={model} section="agents" />} />
        <Route path="/slots" element={<ServerSlots {...slotApi} instances={instances} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        <Route path="/slots/:logicalAgentId" element={<ServerSlots {...slotApi} instances={instances} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        <Route path="/tasks" element={<ServerChooser model={model} section="tasks" />} />
        <Route path="/context" element={<ServerChooser model={model} section="context" />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/connections" element={<Connections key={connectionGeneration} servers={model.servers} diagnostics={diagnostics} />} />
        <Route path="/meshes/:meshId" element={<Connections key={connectionGeneration} servers={model.servers} diagnostics={diagnostics} />} />
        <Route path="/meshes/:meshId/agents" element={<ServerChooser model={model} section="agents" />} />
        <Route path="/meshes/:meshId/slots" element={<ServerSlots {...slotApi} instances={instances} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        <Route path="/meshes/:meshId/tasks" element={<ServerChooser model={model} section="tasks" />} />
        <Route path="/meshes/:meshId/activity" element={<Activity instances={instances} loadActivity={loadActivity} />} />
        <Route path="/meshes/:meshId/slots/:logicalAgentId" element={<ServerSlots {...slotApi} instances={instances} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        <Route path="/ui-kit" element={<UiKitShowcase />} />
        <Route path="/connect" element={<Connections key={connectionGeneration} servers={model.servers} diagnostics={diagnostics} />} />
        <Route path="/servers/:instanceId" element={<ServerWorkspace key={connectionGeneration} model={model} instances={instances} loadFleetControl={loadFleetControl} diagnostics={diagnostics} />} />
        <Route path="/servers/:instanceId/agents" element={<ServerAgents instances={instances} />} />
        <Route path="/servers/:instanceId/agents/:agentId" element={<ServerAgents instances={instances} />} />
        <Route path="/servers/:instanceId/slots" element={<ServerSlots {...slotApi} instances={instances} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        <Route path="/servers/:instanceId/slots/:logicalAgentId" element={<ServerSlots {...slotApi} instances={instances} loadFleetControl={loadFleetControl} mutateFleetControl={mutateFleetControl} />} />
        <Route path="/servers/:instanceId/context" element={<ServerSection model={model} section="context" diagnostics={diagnostics} loadContexts={loadContexts} />} />
        <Route path="/servers/:instanceId/tasks" element={<ServerTasks instances={instances} loadTask={loadTask} />} />
        <Route path="/servers/:instanceId/tasks/:namespace/:taskId" element={<ServerTasks instances={instances} loadTask={loadTask} />} />
        <Route path="/activity" element={<Activity instances={instances} loadActivity={loadActivity} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  )
}
