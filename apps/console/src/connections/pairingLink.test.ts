import { expect,test } from 'vitest'

import {
PRODUCTION_CONSOLE_ORIGIN,
parseCanonicalPairingLink,
} from './pairingLink'

function pairingLink(payload: object): string {
  const json = JSON.stringify(payload)
  const bytes = new TextEncoder().encode(json)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  const encoded = btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  return PRODUCTION_CONSOLE_ORIGIN + '/connect#' + encoded
}

const core = {
  v: 2,
  server: 'https://terminal.example',
  name: 'Terminal',
  code: 'one-time-code-123456789',
}

test('preserves bounded unknown safe profile extensions and ignores unknown top-level fields', () => {
  const parsed = parseCanonicalPairingLink(pairingLink({
    ...core,
    futureTopLevel: { access_token: 'never-persist-this' },
    ephemeral: { nonce: 'transient-only' },
    profile: {
      labelHint: 'Lab',
      clearedHint: null,
      extensions: {
        'vendor.example': {
          capability: 'mesh-v2',
          nested: { level: 2 },
        },
      },
    },
  }))

  expect(parsed.code).toBe(core.code)
  expect(parsed.profile).toEqual({
    labelHint: 'Lab',
    clearedHint: null,
    extensions: {
      'vendor.example': {
        capability: 'mesh-v2',
        nested: { level: 2 },
      },
    },
  })
  expect(JSON.stringify(parsed.profile)).not.toContain('never-persist-this')
  expect(JSON.stringify(parsed.profile)).not.toContain('transient-only')
})

test('rejects a refresh token embedded in a persisted profile extension', () => {
  expect(() => parseCanonicalPairingLink(pairingLink({
    ...core,
    profile: { extensions: { vendor: { refresh_token: 'forbidden' } } },
  }))).toThrowError('invalid_pairing_link')
})
test('rejects prototype-control keys in profile extensions', () => {
  expect(() => parseCanonicalPairingLink(pairingLink({
    ...core,
    profile: { extensions: { vendor: { ['__proto__']: { polluted: true } } } },
  }))).toThrowError('invalid_pairing_link')
})


test('rejects the retired version 1 link', () => {
  expect(() => parseCanonicalPairingLink(pairingLink({
    ...core,
    v: 1,
    secret: core.code,
  }))).toThrowError('unsupported_pairing_version')
})

test('rejects a version 2 link without code', () => {
  const missingCode = { v: core.v, server: core.server, name: core.name }
  expect(() => parseCanonicalPairingLink(pairingLink({
    ...missingCode,
    secret: core.code,
  }))).toThrowError('invalid_pairing_link')
})
