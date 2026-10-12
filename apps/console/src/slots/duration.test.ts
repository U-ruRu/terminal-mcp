import { expect, test } from 'vitest'
import { durationSecondsValue, durationValue } from './duration'

test('hours round-trip as seconds without a session-duration cap', () => {
  expect(durationSecondsValue({ amount: '3', unit: 'hours' })).toBe(10800)
  expect(durationValue(10800)).toEqual({ amount: '3', unit: 'hours' })
  expect(durationValue(90)).toEqual({ amount: '90', unit: 'seconds' })
})

test('invalid, fractional and unsafe integers never become valid policy values', () => {
  for (const amount of ['-1', '1.5', 'NaN', 'Infinity', '1e3', '', '9007199254740992']) {
    expect(durationSecondsValue({ amount, unit: 'seconds' })).toBeNull()
  }
  expect(durationSecondsValue({ amount: String(Number.MAX_SAFE_INTEGER), unit: 'hours' })).toBeNull()
  expect(durationSecondsValue({ amount: '0', unit: 'seconds' })).toBe(0)
})
