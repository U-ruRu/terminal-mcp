export type StoredConnection = {
  origin: string
  deviceId: string
  clientId: string
  deviceLabel: string
  scope: string
  refreshToken: string
  pairedAt: number
}

export type AccessSession = {
  connection: Omit<StoredConnection, 'refreshToken'>
  accessToken: string
  accessExpiresAt: number
}

export type ConnectionState =
  | { status: 'unpaired' }
  | { status: 'pairing' }
  | { status: 'restoring'; connection: Omit<StoredConnection, 'refreshToken'> }
  | { status: 'connected'; session: AccessSession }
  | { status: 'disconnected' }
  | { status: 'revoked'; connection?: Omit<StoredConnection, 'refreshToken'> }
  | { status: 'expired'; connection?: Omit<StoredConnection, 'refreshToken'> }
  | {
      status: 'error'
      operation: 'pair' | 'restore'
      retryable: boolean
      message: string
      connection?: Omit<StoredConnection, 'refreshToken'>
    }

export type PairingExchangeResponse = {
  device_id: string
  client_id: string
  access_token: string
  token_type: string
  expires_in: number
  refresh_token: string
  scope: string
}

export type RefreshResponse = {
  access_token: string
  token_type: string
  expires_in: number
  refresh_token: string
  scope: string
}
