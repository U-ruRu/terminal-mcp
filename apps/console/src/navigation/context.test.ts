import { expect,test } from 'vitest'
import { navigationReturnTo } from './context'

test('external return destinations are rejected', () => {
  expect(navigationReturnTo({ returnTo: 'https://example.com' })).toBeUndefined()
})
