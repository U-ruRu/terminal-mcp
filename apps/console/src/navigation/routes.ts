function part(value: string): string {
  return encodeURIComponent(value)
}

export function serverRoute(instanceId: string): string {
  return `/servers/${part(instanceId)}`
}

export function agentsRoute(instanceId: string): string {
  return `${serverRoute(instanceId)}/agents`
}

export function agentRoute(instanceId: string, agentId: string): string {
  return `${agentsRoute(instanceId)}/${part(agentId)}`
}

export function tasksRoute(instanceId: string): string {
  return `${serverRoute(instanceId)}/tasks`
}

export function taskRoute(instanceId: string, namespace: string, taskId: string): string {
  return `${tasksRoute(instanceId)}/${part(namespace)}/${part(taskId)}`
}

export function contextRoute(instanceId: string): string {
  return `${serverRoute(instanceId)}/context`
}

export function healthRoute(instanceId: string): string {
  return `${serverRoute(instanceId)}/health`
}

export function activityRoute(instanceId: string, options: { agentId?: string } = {}): string {
  const query = new URLSearchParams({ server: instanceId })
  if (options.agentId) query.set('agent', options.agentId)
  return `/activity?${query.toString()}`
}

export function settingsRoute(instanceId?: string): string {
  if (!instanceId) return '/settings'
  return `/settings?server=${part(instanceId)}`
}
