import { beforeEach,describe,expect,test } from 'vitest'

import {
ACCESS_CODE_STORAGE_PREFIX,
loadAccessCode,
saveAccessCode
} from './codeVault'

describe('persistent Access code vault', () => {
  beforeEach(() => localStorage.clear())

  test('preserves four decimal digits including leading zeroes across reloads', () => {
    saveAccessCode('as_alpha', { code: '0042', generation: 3, publicName: 'Alpha' })
    expect(loadAccessCode('as_alpha', 3)).toEqual({
      code: '0042',
      generation: 3,
      publicName: 'Alpha',
    })
    expect(localStorage.getItem(ACCESS_CODE_STORAGE_PREFIX + 'as_alpha')).toContain('0042')
  })

  test('removes a stale local code when authority reports a newer generation', () => {
    saveAccessCode('as_alpha', { code: '0042', generation: 3 })
    expect(loadAccessCode('as_alpha', 4)).toBeNull()
    expect(localStorage.getItem(ACCESS_CODE_STORAGE_PREFIX + 'as_alpha')).toBeNull()
  })

  test('keeps a newly rotated local code while an older projection catches up', () => {
    saveAccessCode('as_alpha', { code: '7319', generation: 4 })
    expect(loadAccessCode('as_alpha', 3)?.code).toBe('7319')
  })
})

  test('native issuer scopes keep the same slot ID on different servers separate', () => {
    saveAccessCode('as_same', { code: '0042', generation: 1 }, localStorage, 'issuer:a:slot:a')
    saveAccessCode('as_same', { code: '7319', generation: 1 }, localStorage, 'issuer:b:slot:b')
    expect(loadAccessCode('as_same', 1, localStorage, 'issuer:a:slot:a')?.code).toBe('0042')
    expect(loadAccessCode('as_same', 1, localStorage, 'issuer:b:slot:b')?.code).toBe('7319')
  })

  test('server-provided expiry expires a stored code without borrowing the session deadline', () => {
    saveAccessCode('as_alpha', { code: '0042', generation: 1, expiresAt: '2000-01-01T00:00:00Z' })
    expect(loadAccessCode('as_alpha', 1)).toBeNull()
  })
