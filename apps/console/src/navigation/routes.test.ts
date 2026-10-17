import { expect,test } from 'vitest'

import { activityRoute } from './routes'

test('activity route preserves server and exact agent identity independently of public name', () => {
  expect(activityRoute('alpha', { agentId: 'SameName-1111' })).toBe(
    '/activity?server=alpha&agent=SameName-1111',
  )
})
