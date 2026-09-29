import { afterEach, describe, expect, test } from 'vitest'

import { catalogs, en, supportedLocales } from './catalogs'
import {
  applyDocumentLocale,
  DEFAULT_LOCALE,
  detectLocale,
  formatNumber,
  initialLocale,
  LOCALE_STORAGE_KEY,
  normalizeLocale,
  pluralCategory,
  translate,
} from './runtime'

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('lang')
})

describe('locale selection', () => {
  test('normalizes supported language tags and rejects unsupported locales', () => {
    expect(normalizeLocale('ka-GE')).toBe('ka')
    expect(normalizeLocale('ES_es')).toBe('es')
    expect(normalizeLocale('de-DE')).toBeNull()
  })

  test('prefers persisted locale, then device languages, then English', () => {
    expect(detectLocale('ru', ['ka-GE'])).toBe('ru')
    expect(detectLocale(null, ['de-DE', 'ka-GE'])).toBe('ka')
    expect(detectLocale(null, ['de-DE'])).toBe(DEFAULT_LOCALE)
  })

  test('reads persisted locale from browser storage', () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'es')
    expect(initialLocale()).toBe('es')
  })
})

test('all four catalogs expose stable typed messages', () => {
  expect(supportedLocales).toEqual(['en', 'ru', 'ka', 'es'])
  for (const locale of supportedLocales) {
    expect(translate(locale, 'nav.tasks')).toBeTruthy()
    expect(translate(locale, 'aria.consoleNavigation')).toBeTruthy()
  }
})

test('every locale has every English key and translation fallback is deterministic', () => {
  const keys = Object.keys(en)
  for (const locale of supportedLocales) {
    expect(Object.keys(catalogs[locale])).toEqual(keys)
    expect(Object.values(catalogs[locale]).every((value) => value.length > 0)).toBe(true)
  }

  const original = catalogs.ru['nav.tasks']
  Reflect.set(catalogs.ru, 'nav.tasks', undefined)
  expect(translate('ru', 'nav.tasks')).toBe(en['nav.tasks'])
  Reflect.set(catalogs.ru, 'nav.tasks', original)
})

test('uses Intl for numbers and plural rules', () => {
  expect(formatNumber('en', 1200)).toMatch(/1,200/)
  expect(pluralCategory('en', 1)).toBe('one')
  expect(pluralCategory('en', 2)).toBe('other')
})

test('applies locale to the document root', () => {
  applyDocumentLocale('ka')
  expect(document.documentElement.lang).toBe('ka')
})
