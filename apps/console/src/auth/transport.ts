import type { PairingExchangeResponse, RefreshResponse } from './types'

export class AuthTransportError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message = 'Authentication request failed',
  ) {
    super(message)
    this.name = 'AuthTransportError'
  }

  get revoked(): boolean {
    return this.code === 'invalid_client'
  }

  get expired(): boolean {
    return this.code === 'invalid_grant'
  }

  get retryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500
  }
}

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

async function responsePayload(response: Response): Promise<Record<string, unknown>> {
  try {
    const payload: unknown = await response.json()
    return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

async function requireJson<T>(response: Response): Promise<T> {
  const payload = await responsePayload(response)
  if (!response.ok) {
    const code = typeof payload.error === 'string' ? payload.error : 'request_failed'
    throw new AuthTransportError(response.status, code)
  }
  return payload as T
}

export class PairingTransport {
  constructor(private readonly fetcher: FetchLike = fetch) {}

  async exchange(
    origin: string,
    secret: string,
    publicKey: string,
    deviceLabel: string,
  ): Promise<PairingExchangeResponse> {
    let response: Response
    try {
      response = await this.fetcher(new URL('/pairing/exchange', origin), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          secret,
          public_key: publicKey,
          device_label: deviceLabel,
        }),
      })
    } catch {
      throw new AuthTransportError(0, 'network_error')
    }
    return requireJson<PairingExchangeResponse>(response)
  }

  async refresh(
    origin: string,
    clientId: string,
    refreshToken: string,
  ): Promise<RefreshResponse> {
    const body = new URLSearchParams({
      grant_type: 'refresh_token',
      client_id: clientId,
      refresh_token: refreshToken,
    })
    let response: Response
    try {
      response = await this.fetcher(new URL('/oauth/token', origin), {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
      })
    } catch {
      throw new AuthTransportError(0, 'network_error')
    }
    return requireJson<RefreshResponse>(response)
  }
}
