import { AuthTransportError, PairingTransport } from './transport'
import { publicConnection, restoreCredential } from './restore'
import type { AccessSession, ConnectionState, StoredConnection } from './types'
import { BrowserCredentialVault } from './vault'

type BrowserLocation = Pick<Location, 'hash' | 'origin' | 'pathname' | 'search'>
type BrowserHistory = Pick<History, 'replaceState'>

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
        message: transportError.message,
      }
    }
    return this.state
  }

  async restore(): Promise<ConnectionState> {
    const current = this.vault.load()
    if (current) {
      this.state = { status: 'restoring', connection: publicConnection(current) }
    } else {
      this.state = { status: 'unpaired' }
    }

    const restored = await restoreCredential(this.vault, this.transport, this.now)
    switch (restored.status) {
      case 'connected':
        this.state = {
          status: 'connected',
          session: {
            connection: restored.connection,
            accessToken: restored.accessToken,
            accessExpiresAt: restored.accessExpiresAt,
          },
        }
        break
      case 'unpaired':
        this.state = { status: 'unpaired' }
        break
      case 'revoked':
      case 'expired':
        this.state = { status: restored.status, connection: restored.connection }
        break
      case 'error':
        this.state = {
          status: 'error',
          operation: 'restore',
          retryable: restored.retryable,
          message: restored.message,
          connection: restored.connection,
        }
        break
    }
    return this.state
  }

  disconnect(): ConnectionState {
    this.vault.clear()
    this.state = { status: 'disconnected' }
    return this.state
  }
}
