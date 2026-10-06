import { expect, test } from 'vitest'

import {
  connectionLifecycle,
  pairingErrorMessageKey,
} from '../connections/lifecycle'

test('projects generic connection lifecycle from auth state and control freshness', () => {
  expect(connectionLifecycle({ status: 'restoring' }, undefined)).toBe('connecting')
  expect(connectionLifecycle({ status: 'restoring' }, {
    freshness: 'stale',
    control: {} as never,
  })).toBe('reconnecting')
  expect(connectionLifecycle({
    status: 'connected',
    accessToken: 'access',
    accessExpiresAt: 1,
  }, { freshness: 'fresh' })).toBe('live')
  expect(connectionLifecycle({
    status: 'connected',
    accessToken: 'access',
    accessExpiresAt: 1,
  }, { freshness: 'stale', control: {} as never })).toBe('stale')
  expect(connectionLifecycle({
    status: 'connected',
    accessToken: 'access',
    accessExpiresAt: 1,
  }, { freshness: 'unknown', error: 'fleet_control_unavailable' })).toBe('live')
  expect(connectionLifecycle({
    status: 'error',
    retryable: true,
    message: 'timeout',
  }, undefined)).toBe('offline')
  expect(connectionLifecycle({
    status: 'error',
    retryable: false,
    message: 'incompatible',
  }, undefined)).toBe('attention')
})

test('maps pairing failures to stable localized message keys without reflecting raw detail', () => {
  expect(pairingErrorMessageKey(new Error('unsupported_pairing_version')))
    .toBe('connections.pairingUnsupported')
  expect(pairingErrorMessageKey(new Error('invalid_pairing_link')))
    .toBe('connections.pairingInvalid')
  expect(pairingErrorMessageKey(new Error('duplicate_origin')))
    .toBe('connections.pairingDuplicate')
  expect(pairingErrorMessageKey(new Error('secret=must-never-surface')))
    .toBe('connections.pairingFailed')
})
