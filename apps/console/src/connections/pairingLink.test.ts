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
  v: 1,
  server: 'https://terminal.example',
  name: 'Terminal',
  secret: 'one-time-secret-123456789',
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
