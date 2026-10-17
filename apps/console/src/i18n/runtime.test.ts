import { afterEach,describe,expect,test } from 'vitest'

import {
applyDocumentLocale,
detectLocale
} from './runtime'

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('lang')
})

describe('locale selection', () => {

  test('a persisted locale takes priority over device languages', () => {
    expect(detectLocale('ru', ['ka-GE'])).toBe('ru')
  })
})

test('applies locale to the document root', () => {
  applyDocumentLocale('ka')
  expect(document.documentElement.lang).toBe('ka')
})
