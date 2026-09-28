import { AuthTransportError, PairingTransport } from './transport'
import type { AccessSession, ConnectionState, StoredConnection } from './types'
import { BrowserCredentialVault } from './vault'

type BrowserLocation = Pick<Location, 'hash' | 'origin' | 'pathname' | 'search'>
type BrowserHistory = Pick<History, 'replaceState'>

function publicConnection(
  connection: StoredConnection,
): Omit<StoredConnection, 'refreshToken'> {
  return {
    origin: connection.origin,
    deviceId: connection.deviceId,
    clientId: connection.clientId,
    deviceLabel: connection.deviceLabel,
    scope: connection.scope,
    pairedAt: connection.pairedAt,
  }
}

function sessionFrom(
  connection: StoredConnection,
  accessToken: string,
  expiresIn: number,
  now: number,
): AccessSession {
  return {
    connection: publicConnection(connection),
    accessToken,
    accessExpiresAt: now + Math.max(0, expiresIn) * 1000,
  }
}

export async function generateDevicePublicKey(): Promise<string> {
  const pair = await crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    true,
    ['sign', 'verify'],
  )
  const encoded = new Uint8Array(await crypto.subtle.exportKey('spki', pair.publicKey))
  let binary = ''
  for (const byte of encoded) binary += String.fromCharCode(byte)
  return btoa(binary)
}

export class ConnectionManager {
  state: ConnectionState = { status: 'unpaired' }

  constructor(
    private readonly vault = new BrowserCredentialVault(),
    private readonly transport = new PairingTransport(),
    private readonly now: () => number = Date.now,
    private readonly keyFactory: () => Promise<string> = generateDevicePublicKey,
  ) {}

  async pairFromFragment(
    location: BrowserLocation,
    history: BrowserHistory,
    deviceLabel: string,
  ): Promise<ConnectionState> {
    const secret = location.hash.startsWith('#') ? location.hash.slice(1) : ''
    if (!secret) {
      this.state = { status: 'unpaired' }
      return this.state
    }

    const origin = new URL(location.origin).origin
    // Erase the one-time secret before crypto or network activity can fail.
    history.replaceState(null, '', `${location.pathname}${location.search}`)
    this.state = { status: 'pairing' }

    try {
      const publicKey = await this.keyFactory()
      const exchanged = await this.transport.exchange(origin, secret, publicKey, deviceLabel)
      const stored: StoredConnection = {
        origin,
        deviceId: exchanged.device_id,
        clientId: exchanged.client_id,
        deviceLabel,
        scope: exchanged.scope,
        refreshToken: exchanged.refresh_token,
        pairedAt: this.now(),
      }
      this.vault.save(stored)
      this.state = {
        status: 'connected',
        session: sessionFrom(stored, exchanged.access_token, exchanged.expires_in, this.now()),
      }
    } catch (error) {
      const transportError =
        error instanceof AuthTransportError
          ? error
          : new AuthTransportError(0, 'pairing_failed')
      this.state = {
        status: 'error',
        operation: 'pair',
        retryable: transportError.retryable,
        message: transportError.code,
      }
    }
    return this.state
  }

  async restore(): Promise<ConnectionState> {
    const stored = this.vault.load()
    if (!stored) {
      this.state = { status: 'unpaired' }
      return this.state
    }

    const publicFields = publicConnection(stored)
    this.state = { status: 'restoring', connection: publicFields }
    try {
      const refreshed = await this.transport.refresh(
        stored.origin,
        stored.clientId,
        stored.refreshToken,
      )
      const rotated: StoredConnection = {
        ...stored,
        scope: refreshed.scope,
        refreshToken: refreshed.refresh_token,
      }
      this.vault.save(rotated)
      this.state = {
        status: 'connected',
        session: sessionFrom(rotated, refreshed.access_token, refreshed.expires_in, this.now()),
      }
    } catch (error) {
      const transportError =
        error instanceof AuthTransportError
          ? error
          : new AuthTransportError(0, 'restore_failed')
      if (transportError.revoked) {
        this.vault.clear()
        this.state = { status: 'revoked', connection: publicFields }
      } else if (transportError.expired) {
        this.vault.clear()
        this.state = { status: 'expired', connection: publicFields }
      } else {
        this.state = {
          status: 'error',
          operation: 'restore',
          retryable: transportError.retryable,
          message: transportError.code,
          connection: publicFields,
        }
      }
    }
    return this.state
  }

  disconnect(): ConnectionState {
    this.vault.clear()
    this.state = { status: 'disconnected' }
    return this.state
  }
}
