import { expect, test } from 'vitest'
import { navigationReturnTo, serverSwitchDestination } from './context'

test('server switching preserves the current server-scoped surface', () => {
  expect(serverSwitchDestination('/servers/tokyo', '', 'firstbyte')).toBe('/servers/firstbyte')
  expect(serverSwitchDestination('/servers/tokyo/agents', '', 'firstbyte')).toBe('/servers/firstbyte/agents')
  expect(serverSwitchDestination('/servers/tokyo/agents/a-1', '', 'firstbyte')).toBe('/servers/firstbyte/agents')
  expect(serverSwitchDestination('/servers/tokyo/tasks', '', 'firstbyte')).toBe('/servers/firstbyte/tasks')
  expect(serverSwitchDestination('/servers/tokyo/tasks/ns/T-1', '', 'firstbyte')).toBe('/servers/firstbyte/tasks')
  expect(serverSwitchDestination('/servers/tokyo/context', '', 'firstbyte')).toBe('/servers/firstbyte/context')
  expect(serverSwitchDestination('/servers/tokyo/health', '', 'firstbyte')).toBe('/servers/firstbyte/health')
  expect(serverSwitchDestination('/activity', '?server=tokyo', 'firstbyte')).toBe('/activity?server=firstbyte')
})

test('entity return context accepts only application-local destinations', () => {
  expect(navigationReturnTo({ returnTo: '/activity?server=tokyo' })).toBe('/activity?server=tokyo')
  expect(navigationReturnTo({ returnTo: 'https://example.com' })).toBeUndefined()
  expect(navigationReturnTo(null)).toBeUndefined()
})
