import { afterEach,describe,expect,test } from 'vitest'

import {
applyThemeToDocument,
themeMetaColor
} from './theme'

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.documentElement.removeAttribute('style')
  document.querySelector('meta[name="theme-color"]')?.remove()
})

describe('theme preference', () => {

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
