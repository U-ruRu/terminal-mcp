import { expect, test } from 'vitest'

import { activityRoute, agentRoute, contextRoute, healthRoute, serverRoute, taskRoute } from './routes'

test('builds canonical server-scoped entity routes from stable identities', () => {
  expect(serverRoute('server / one')).toBe('/servers/server%20%2F%20one')
  expect(taskRoute('server / one', 'ops/core', 'M3 009')).toBe(
    '/servers/server%20%2F%20one/tasks/ops%2Fcore/M3%20009',
  )
  expect(agentRoute('server / one', 'Alpha-AB/CD')).toBe(
    '/servers/server%20%2F%20one/agents/Alpha-AB%2FCD',
  )
  expect(contextRoute('server / one')).toBe('/servers/server%20%2F%20one/context')
  expect(healthRoute('server / one')).toBe('/servers/server%20%2F%20one/health')
})

test('activity route preserves server and exact agent identity independently of public name', () => {
  expect(activityRoute('alpha', { agentId: 'SameName-1111' })).toBe(
    '/activity?server=alpha&agent=SameName-1111',
  )
  expect(activityRoute('alpha', { agentId: 'SameName-2222' })).toBe(
    '/activity?server=alpha&agent=SameName-2222',
  )
})
