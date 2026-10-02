import type { PairingTransport } from './transport'
import { AuthTransportError } from './transport'
import type { StoredConnection } from './types'
import { BrowserCredentialVault } from './vault'

export type PublicStoredConnection = Omit<StoredConnection, 'refreshToken'>

export type CredentialRestoreResult =
  | {
      status: 'connected'
      connection: PublicStoredConnection
      accessToken: string
      accessExpiresAt: number
    }
  | { status: 'unpaired' }
  | { status: 'revoked' | 'expired'; connection: PublicStoredConnection }
  | {
      status: 'error'
      connection?: PublicStoredConnection
      retryable: boolean
      message: string
    }

const MAX_CREDENTIAL_CHANGE_RETRIES = 3

export function publicConnection(
  connection: StoredConnection,
): PublicStoredConnection {
  return {
    origin: connection.origin,
    deviceId: connection.deviceId,
    clientId: connection.clientId,
    deviceLabel: connection.deviceLabel,
    scope: connection.scope,
    pairedAt: connection.pairedAt,
  }
}

export function restoreCredential(
  vault: BrowserCredentialVault,
  transport: Pick<PairingTransport, 'refresh'>,
  now: () => number = Date.now,
): Promise<CredentialRestoreResult> {
  return vault.restoreSingleFlight(async () => {
    for (let attempt = 0; attempt < MAX_CREDENTIAL_CHANGE_RETRIES; attempt += 1) {
      const stored = vault.load()
      if (!stored) return { status: 'unpaired' }

      const connection = publicConnection(stored)
      try {
        const refreshed = await transport.refresh(
          stored.origin,
          stored.clientId,
          stored.refreshToken,
        )
        const rotated: StoredConnection = {
          ...stored,
          scope: refreshed.scope,
          refreshToken: refreshed.refresh_token,
        }

        // Pair/disconnect can replace credentials while a network refresh is in flight.
        // Never let an older response overwrite a newer local generation.
        if (!vault.replaceIfCurrent(stored, rotated)) continue

        return {
          status: 'connected',
          connection: publicConnection(rotated),
          accessToken: refreshed.access_token,
          accessExpiresAt: now() + Math.max(0, refreshed.expires_in) * 1000,
        }
      } catch (error) {
        const transportError =
          error instanceof AuthTransportError
            ? error
            : new AuthTransportError(0, 'restore_failed')

        if (transportError.revoked || transportError.expired) {
          // A stale one-time refresh token can fail after another consumer rotated it.
          // Clear only the exact generation that failed; retry if storage advanced.
          if (!vault.clearIfCurrent(stored)) continue
          return {
            status: transportError.revoked ? 'revoked' : 'expired',
            connection,
          }
        }

        return {
          status: 'error',
          connection,
          retryable: transportError.retryable,
          message: transportError.message,
        }
      }
    }

    const current = vault.load()
    return {
      status: 'error',
      connection: current ? publicConnection(current) : undefined,
      retryable: true,
      message: 'Authentication changed while restoring. Try again.',
    }
  })
}
