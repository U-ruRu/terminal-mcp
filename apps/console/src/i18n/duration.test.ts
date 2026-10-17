import { describe,expect,test } from 'vitest'
import { parseDuration } from './duration'

describe('localized human durations', () => {
  test.each([
    ['ru', '3 минуты'],
    ['ka', '3 წუთი'],
    ['es', '3 minutos'],
  ] as const)('%s parses localized policy input without changing effective seconds', (locale, minutes) => {
    expect(parseDuration(minutes, locale)).toBe(180)
  })

})
