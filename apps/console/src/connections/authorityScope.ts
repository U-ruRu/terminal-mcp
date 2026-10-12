import type { ConnectionProfile } from './types'

/** Non-secret identity for cached reads. Re-pairing never reuses a previous authorization. */
export function connectionAuthorityScope(profile: ConnectionProfile): string {
  return JSON.stringify([
    profile.instanceId, profile.origin.replace(/\/+$/, ''), profile.credentialRef,
    profile.metadata.deviceId, profile.metadata.clientId, profile.metadata.pairedAt,
    profile.metadata.scope.trim().split(/\s+/).sort(),
  ])
}
