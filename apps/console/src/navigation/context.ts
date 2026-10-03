export type EntityNavigationState = {
  returnTo?: string
}

function decodedServerPath(pathname: string): { tail: string[] } | undefined {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] !== 'servers' || !parts[1]) return undefined
  return { tail: parts.slice(2) }
}

export function navigationReturnTo(state: unknown): string | undefined {
  if (!state || typeof state !== 'object') return undefined
  const value = (state as EntityNavigationState).returnTo
  return typeof value === 'string' && value.startsWith('/') ? value : undefined
}

export function returnToState(pathname: string, search = ''): EntityNavigationState {
  return { returnTo: pathname + search }
}

export function serverSwitchDestination(pathname: string, _search: string, instanceId: string): string {
  const encoded = encodeURIComponent(instanceId)
  if (pathname === '/activity') return '/activity?server=' + encoded

  const scoped = decodedServerPath(pathname)
  if (!scoped) return '/servers/' + encoded
  const section = scoped.tail[0]
  if (!section) return '/servers/' + encoded
  if (section === 'agents') return '/servers/' + encoded + '/agents'
  if (section === 'tasks') return '/servers/' + encoded + '/tasks'
  if (section === 'slots') return '/servers/' + encoded + '/slots'
  if (section === 'context') return '/servers/' + encoded + '/context'
  if (section === 'health') return '/servers/' + encoded + '/health'
  return '/servers/' + encoded
}
