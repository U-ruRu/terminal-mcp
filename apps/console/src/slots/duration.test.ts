import { expect,test } from 'vitest'
import { durationSecondsValue } from './duration'

test('a three-hour policy input converts to10800 seconds', () => {
  expect(durationSecondsValue({ amount: '3', unit: 'hours' })).toBe(10800)
})
