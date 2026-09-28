import type { StoredConnection } from '../auth/types'

export type ConnectionProfileMetadata = Pick<
  StoredConnection,
  'deviceId' | 'clientId' | 'deviceLabel' | 'scope' | 'pairedAt'
>

export type ConnectionProfile = {
  instanceId: string
  origin: string
  displayName: string
  credentialRef: string
  metadata: ConnectionProfileMetadata
  createdAt: number
  updatedAt: number
}

export type ConnectionRegistryDocument = {
  version: 1
  profiles: ConnectionProfile[]
}

export type PairedProfile = {
  profile: ConnectionProfile
  accessToken: string
  accessExpiresAt: number
}
