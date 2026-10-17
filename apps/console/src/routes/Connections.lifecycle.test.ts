import { expect,test } from 'vitest'

import { connectionLifecycle } from '../connections/lifecycle'

test('a connected server with a fresh control read is live', () => {
  const state = { status: 'connected' as const, accessToken: 'access', accessExpiresAt: 1 }

  expect(connectionLifecycle(state, { freshness: 'fresh' })).toBe('live')
})
