import { beforeEach, describe, expect, test } from 'vitest'

import {
  ACCESS_CODE_STORAGE_PREFIX,
  clearAccessCode,
  loadAccessCode,
  saveAccessCode,
} from './codeVault'

describe('persistent Access code vault', () => {
  beforeEach(() => localStorage.clear())

  test('preserves four decimal digits including leading zeroes across reloads', () => {
    saveAccessCode('la_alpha', { code: '0042', generation: 3, publicName: 'Alpha' })
    expect(loadAccessCode('la_alpha', 3)).toEqual({
      code: '0042',
      generation: 3,
      publicName: 'Alpha',
    })
    expect(localStorage.getItem(ACCESS_CODE_STORAGE_PREFIX + 'la_alpha')).toContain('0042')
  })

  test('rejects non-decimal or wrong-length values', () => {
    expect(() => saveAccessCode('la_alpha', { code: 'ABCD', generation: 1 })).toThrow(
      'invalid_access_code',
    )
    expect(() => saveAccessCode('la_alpha', { code: '123', generation: 1 })).toThrow(
      'invalid_access_code',
    )
  })

  test('removes a stale local code when authority reports a newer generation', () => {
    saveAccessCode('la_alpha', { code: '0042', generation: 3 })
    expect(loadAccessCode('la_alpha', 4)).toBeNull()
    expect(localStorage.getItem(ACCESS_CODE_STORAGE_PREFIX + 'la_alpha')).toBeNull()
  })

  test('keeps a newly rotated local code while an older projection catches up', () => {
    saveAccessCode('la_alpha', { code: '7319', generation: 4 })
    expect(loadAccessCode('la_alpha', 3)?.code).toBe('7319')
    clearAccessCode('la_alpha')
    expect(loadAccessCode('la_alpha', 4)).toBeNull()
  })
})
