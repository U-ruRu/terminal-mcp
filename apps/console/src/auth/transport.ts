import {
  Capacitor,
  CapacitorHttp,
  type HttpOptions,
  type HttpResponse,
} from '@capacitor/core'

import type { PairingExchangeResponse, RefreshResponse } from './types'

const NATIVE_CONNECT_TIMEOUT_MS = 15_000
const NATIVE_READ_TIMEOUT_MS = 15_000

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
export type NativeHttpLike = Pick<typeof CapacitorHttp, 'request'>
export type NativeAndroidDetector = () => boolean

type AuthResponse = {
  status: number
  payload: Record<string, unknown>
}

function safeDiagnosticCode(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback
  const normalized = value.trim().toLowerCase()
  return /^[a-z0-9][a-z0-9_.:-]{0,63}$/.test(normalized) ? normalized : fallback
}

function userMessage(code: string, status: number): string {
  switch (code) {
    case 'network_error':
      return 'Cannot reach the server. Check the server address and network connection. Diagnostic: network_error.'
    case 'dns_error':
      return 'Cannot resolve the server address. Check the hostname or DNS connection. Diagnostic: dns_error.'
    case 'timeout':
      return 'The server did not respond in time. Check connectivity and try again. Diagnostic: timeout.'
    case 'tls_error':
      return 'Secure connection validation failed. Check the server certificate and device clock. Diagnostic: tls_error.'
    case 'cleartext_blocked':
      return 'Android blocked an insecure HTTP connection. Use an HTTPS Terminal MCP address. Diagnostic: cleartext_blocked.'
    case 'invalid_pairing':
      return 'The pairing link is invalid, expired, or already used. Generate a new pairing link. Diagnostic: invalid_pairing.'
    case 'invalid_client':
      return 'This saved device pairing was revoked. Pair this server again. Diagnostic: invalid_client.'
    case 'invalid_grant':
      return 'Saved authentication material expired. Pair this server again. Diagnostic: invalid_grant.'
    case 'temporarily_unavailable':
      return 'The server is temporarily unavailable. Try again later. Diagnostic: temporarily_unavailable.'
    default:
      return status > 0
        ? `Authentication failed with HTTP ${status}. Diagnostic: ${code}.`
        : `Authentication failed before an HTTP response. Diagnostic: ${code}.`
  }
}

export class AuthTransportError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message = userMessage(code, status),
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
    return (
      ['network_error', 'dns_error', 'timeout'].includes(this.code)
      || this.status === 429
      || this.status >= 500
    )
  }
}

function recordPayload(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : {}
    } catch {
      return {}
    }
  }
  return {}
}

async function browserResponse(response: Response): Promise<AuthResponse> {
  try {
    const parsed: unknown = await response.json()
    return { status: response.status, payload: recordPayload(parsed) }
  } catch {
    return { status: response.status, payload: {} }
  }
}

function nativeResponse(response: HttpResponse): AuthResponse {
  return { status: response.status, payload: recordPayload(response.data) }
}

function nativeFailure(error: unknown): AuthTransportError {
  const candidate =
    error && typeof error === 'object'
      ? error as { code?: unknown; name?: unknown; message?: unknown }
      : {}
  const fingerprint = [candidate.code, candidate.name, candidate.message]
    .filter((value): value is string => typeof value === 'string')
    .join(' ')
    .toLowerCase()

  if (
    fingerprint.includes('ssl')
    || fingerprint.includes('certificate')
    || fingerprint.includes('certpath')
    || fingerprint.includes('handshake')
    || fingerprint.includes('trust anchor')
  ) {
    return new AuthTransportError(0, 'tls_error')
  }
  if (
    fingerprint.includes('unknownhost')
    || fingerprint.includes('unknown host')
    || fingerprint.includes('dns')
    || fingerprint.includes('resolve')
  ) {
    return new AuthTransportError(0, 'dns_error')
  }
  if (fingerprint.includes('timeout') || fingerprint.includes('timed out')) {
    return new AuthTransportError(0, 'timeout')
  }
  if (fingerprint.includes('cleartext')) {
    return new AuthTransportError(0, 'cleartext_blocked')
  }
  return new AuthTransportError(0, 'network_error')
}

function requireJson<T>(response: AuthResponse): T {
  if (response.status < 200 || response.status >= 300) {
    const code = safeDiagnosticCode(
      response.payload.error,
      response.status > 0 ? `http_${response.status}` : 'request_failed',
    )
    throw new AuthTransportError(response.status, code)
  }
  return response.payload as T
}

function defaultNativeAndroid(): boolean {
  return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android'
}

export class PairingTransport {
  constructor(
    private readonly fetcher: FetchLike = fetch,
    private readonly nativeHttp: NativeHttpLike = CapacitorHttp,
    private readonly isNativeAndroid: NativeAndroidDetector = defaultNativeAndroid,
  ) {}

  private async request(
    url: URL,
    browserInit: RequestInit,
    nativeOptions: Omit<HttpOptions, 'url' | 'method'>,
  ): Promise<AuthResponse> {
    if (this.isNativeAndroid()) {
      try {
        const response = await this.nativeHttp.request({
          ...nativeOptions,
          url: url.toString(),
          method: browserInit.method ?? 'POST',
          connectTimeout: NATIVE_CONNECT_TIMEOUT_MS,
          readTimeout: NATIVE_READ_TIMEOUT_MS,
          responseType: 'json',
        })
        return nativeResponse(response)
      } catch (error) {
        throw nativeFailure(error)
      }
    }

    try {
      return await browserResponse(await this.fetcher(url, browserInit))
    } catch {
      throw new AuthTransportError(0, 'network_error')
    }
  }

  async exchange(
    origin: string,
    secret: string,
    publicKey: string,
    deviceLabel: string,
  ): Promise<PairingExchangeResponse> {
    const data = {
      secret,
      public_key: publicKey,
      device_label: deviceLabel,
    }
    const response = await this.request(
      new URL('/pairing/exchange', origin),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      },
      {
        headers: { 'Content-Type': 'application/json' },
        data,
      },
    )
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
    }).toString()
    const response = await this.request(
      new URL('/oauth/token', origin),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
      },
      {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        data: body,
      },
    )
    return requireJson<RefreshResponse>(response)
  }
}
