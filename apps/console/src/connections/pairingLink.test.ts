import { expect, test } from 'vitest'

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

test('accepts old minimal v1 and supplies an empty safe extension envelope', () => {
  expect(parseCanonicalPairingLink(pairingLink(core))).toEqual({
    origin: 'https://terminal.example',
    name: 'Terminal',
    secret: core.secret,
    profile: { extensions: {} },
  })
})

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

test('rejects unsupported major distinctly before pairing', () => {
  expect(() => parseCanonicalPairingLink(pairingLink({ ...core, v: 2 })))
    .toThrowError('unsupported_pairing_version')
})

test('rejects secret-bearing, malformed and oversized safe extension data', () => {
  expect(() => parseCanonicalPairingLink(pairingLink({
    ...core,
    profile: { extensions: { vendor: { refresh_token: 'forbidden' } } },
  }))).toThrowError('invalid_pairing_link')

  expect(() => parseCanonicalPairingLink(pairingLink({
    ...core,
    profile: { extensions: 'not-an-object' },
  }))).toThrowError('invalid_pairing_link')

  expect(() => parseCanonicalPairingLink(pairingLink({
    ...core,
    profile: { extensions: { vendor: 'x'.repeat(1025) } },
  }))).toThrowError('invalid_pairing_link')
})

test('rejects encoded pairing fragments above the hard wire bound', () => {
  const oversized = PRODUCTION_CONSOLE_ORIGIN + '/connect#' + 'A'.repeat(8193)
  expect(() => parseCanonicalPairingLink(oversized)).toThrowError('invalid_pairing_link')
})
