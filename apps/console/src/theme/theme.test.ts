import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  applyThemeToDocument,
  DEFAULT_THEME,
  isThemeName,
  persistTheme,
  readStoredTheme,
  THEME_STORAGE_KEY,
  themeMetaColor,
} from './theme'

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.documentElement.removeAttribute('style')
  document.querySelector('meta[name="theme-color"]')?.remove()
})

describe('theme preference', () => {
  test('accepts only supported theme names and falls back safely', () => {
    expect(isThemeName('oled-dark')).toBe(true)
    expect(isThemeName('soft-light')).toBe(true)
    expect(isThemeName('dark')).toBe(false)
    localStorage.setItem(THEME_STORAGE_KEY, 'unknown')
    expect(readStoredTheme()).toBe(DEFAULT_THEME)
  })

  test('persists a valid preference without depending on storage availability', () => {
    persistTheme('soft-light')
    expect(readStoredTheme()).toBe('soft-light')
    const blockedStorage = { getItem: vi.fn(() => { throw new Error('blocked') }), setItem: vi.fn(() => { throw new Error('blocked') }) }
    expect(readStoredTheme(blockedStorage)).toBe(DEFAULT_THEME)
    expect(() => persistTheme('oled-dark', blockedStorage)).not.toThrow()
  })

  test('applies document semantics and browser chrome color', () => {
    const meta = document.createElement('meta')
    meta.name = 'theme-color'
    document.head.append(meta)
    applyThemeToDocument('soft-light')
    expect(document.documentElement.dataset.theme).toBe('soft-light')
    expect(document.documentElement.style.colorScheme).toBe('light')
    expect(meta.content).toBe(themeMetaColor('soft-light'))
  })
})
