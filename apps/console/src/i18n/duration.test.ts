import { describe, expect, test } from 'vitest'
import { formatDuration, parseDuration } from './duration'

describe('localized human durations', () => {
  test.each([
    ['ru', '3 минуты', '15 секунд'],
    ['ka', '3 წუთი', '15 წამი'],
    ['es', '3 minutos', '15 segundos'],
  ] as const)('%s parses localized policy input without changing effective seconds', (locale, minutes, seconds) => {
    expect(parseDuration(minutes, locale)).toBe(180)
    expect(parseDuration(seconds, locale)).toBe(15)
    expect(formatDuration(180, locale)).not.toMatch(/\b(hr|sec)\b/i)
  })

  test('keeps English policy input compatible', () => {
    expect(parseDuration('1 hr 2 min 3 sec', 'en')).toBe(3723)
    expect(parseDuration('23', 'ru')).toBe(1380)
  })
})
