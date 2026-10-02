import { expect, test, vi } from 'vitest'

import type { ConnectionProfile, ProfileRestoreResult } from '../connections/types'
import { BrowserDirectAuthorityClient } from './directAuthority'

const profile: ConnectionProfile = {
  instanceId: 'tokyo',
  origin: 'https://terminal-tokyo.example',
  displayName: 'Tokyo',
  credentialRef: 'profile-tokyo',
  metadata: {
    deviceId: 'device-tokyo',
    clientId: 'client-tokyo',
    deviceLabel: 'Android console',
    scope: 'terminal:read',
    pairedAt: 1,
  },
  createdAt: 1,
  updatedAt: 1,
}

test('reports a missing local credential as unpaired rather than revoked', async () => {
  const restore = vi.fn<() => Promise<ProfileRestoreResult>>(async () => ({
    status: 'unpaired',
    profile,
  }))
  const fetcher = vi.fn()
  const client = new BrowserDirectAuthorityClient({ restore }, fetcher)

  await expect(client.activity('tokyo')).rejects.toThrow('direct_authority_auth_unpaired')
  expect(restore).toHaveBeenCalledWith('tokyo')
  expect(fetcher).not.toHaveBeenCalled()
})

test('retries one direct-authority request with a refreshed shared session after HTTP 401', async () => {
  let restoreCall = 0
  const restore = vi.fn(async (): Promise<ProfileRestoreResult> => {
    restoreCall += 1
    return {
      status: 'connected', profile,
      accessToken: restoreCall === 1 ? 'access-old' : 'access-new',
      accessExpiresAt: 100_000,
    }
  })
  const invalidateAccessSession = vi.fn()
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (fetcher.mock.calls.length === 1) {
      expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer access-old')
      return new Response(JSON.stringify({ error: 'unauthorized' }), { status: 401, headers: { 'Content-Type': 'application/json' } })
    }
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer access-new')
    return new Response(JSON.stringify({ enrollment: {
      node_id: 'tokyo', origin: profile.origin, public_key: 'public', auth_token: 'mesh-token',
    } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  const client = new BrowserDirectAuthorityClient({ restore, invalidateAccessSession }, fetcher)

  await expect(client.fleetEnrollment('tokyo')).resolves.toMatchObject({ nodeId: 'tokyo' })
  expect(invalidateAccessSession).toHaveBeenCalledTimes(1)
  expect(restore).toHaveBeenCalledTimes(2)
  expect(fetcher).toHaveBeenCalledTimes(2)
})
