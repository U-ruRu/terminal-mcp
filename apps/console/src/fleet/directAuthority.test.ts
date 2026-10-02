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
