import type { StoredConnection } from '../auth/types'
import type { SafePairingProfile } from './pairingLink'

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
  pairingProfile?: SafePairingProfile | null
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

export type ProfileRestoreResult =
  | {
      status: 'connected'
      profile: ConnectionProfile
      accessToken: string
      accessExpiresAt: number
    }
  | { status: 'missing' }
  | { status: 'unpaired'; profile: ConnectionProfile }
  | { status: 'revoked' | 'expired'; profile: ConnectionProfile }
  | {
      status: 'error'
      profile: ConnectionProfile
      retryable: boolean
      message: string
    }
