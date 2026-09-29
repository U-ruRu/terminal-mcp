import type { HttpOptions, HttpResponse } from '@capacitor/core'
import { expect, test, vi } from 'vitest'

import { AuthTransportError, PairingTransport, type NativeHttpLike } from './transport'

const exchanged = {
  device_id: 'dev-test',
  client_id: 'client-test',
  access_token: 'access-test',
  token_type: 'Bearer',
  expires_in: 900,
  refresh_token: 'refresh-test',
  scope: 'terminal:read',
}

function nativeClient(
  implementation: (options: HttpOptions) => Promise<HttpResponse>,
): NativeHttpLike {
  return { request: vi.fn(implementation) }
}

function nativeResponse(data: unknown, status = 200): HttpResponse {
  return { data, status, headers: {}, url: 'https://terminal.example' }
}

test('browser transport keeps using fetch and never invokes native HTTP', async () => {
  const fetcher = vi.fn(async () =>
    new Response(JSON.stringify(exchanged), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  const native = nativeClient(async () => {
    throw new Error('native transport must not run')
  })

  const result = await new PairingTransport(fetcher, native, () => false).exchange(
    'https://terminal.example',
    'one-time-secret',
    'public-key',
    'Browser console',
  )

  expect(result).toEqual(exchanged)
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(native.request).not.toHaveBeenCalled()
})

test('Android pairing uses native HTTP instead of WebView fetch', async () => {
  const fetcher = vi.fn(async () => {
    throw new Error('browser fetch must not run')
  })
  const native = nativeClient(async (options) => {
    expect(options.url).toBe('https://terminal.example/pairing/exchange')
    expect(options.method).toBe('POST')
    expect(options.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(options.data).toEqual({
      secret: 'one-time-secret',
      public_key: 'public-key',
      device_label: 'Android console',
    })
    expect(options.connectTimeout).toBe(15_000)
    expect(options.readTimeout).toBe(15_000)
    expect(options.responseType).toBe('json')
    return nativeResponse(exchanged)
  })

  const result = await new PairingTransport(fetcher, native, () => true).exchange(
    'https://terminal.example',
    'one-time-secret',
    'public-key',
    'Android console',
  )

  expect(result).toEqual(exchanged)
  expect(fetcher).not.toHaveBeenCalled()
  expect(native.request).toHaveBeenCalledTimes(1)
})

test('Android refresh uses native form request and rotates credentials response', async () => {
  const fetcher = vi.fn()
  const native = nativeClient(async (options) => {
    expect(options.url).toBe('https://terminal.example/oauth/token')
    expect(options.headers).toEqual({
      'Content-Type': 'application/x-www-form-urlencoded',
    })
    expect(options.data).toBe(
      'grant_type=refresh_token&client_id=client-test&refresh_token=refresh-old',
    )
    return nativeResponse({
      access_token: 'access-next',
      token_type: 'Bearer',
      expires_in: 600,
      refresh_token: 'refresh-next',
      scope: 'terminal:read',
    })
  })

  const result = await new PairingTransport(fetcher, native, () => true).refresh(
    'https://terminal.example',
    'client-test',
    'refresh-old',
  )

  expect(result.refresh_token).toBe('refresh-next')
  expect(fetcher).not.toHaveBeenCalled()
})

test.each([
  ['SSLHandshakeException certificate verify failed', 'tls_error', false],
  ['java.net.UnknownHostException: private-host.example', 'dns_error', true],
  ['java.net.SocketTimeoutException: timed out', 'timeout', true],
  ['CLEARTEXT communication not permitted', 'cleartext_blocked', false],
  ['opaque native failure', 'network_error', true],
])(
  'native failures map to safe actionable diagnostics: %s',
  async (nativeMessage, code, retryable) => {
    const native = nativeClient(async () => {
      throw new Error(nativeMessage)
    })
    const transport = new PairingTransport(vi.fn(), native, () => true)

    const failure = await transport
      .exchange(
        'https://terminal.example',
        'must-not-leak-secret',
        'public-key',
        'Android console',
      )
      .then(
        () => null,
        (error: unknown) => error,
      )

    expect(failure).toBeInstanceOf(AuthTransportError)
    expect(failure).toMatchObject({ code, retryable })
    expect((failure as Error).message).toContain(`Diagnostic: ${code}`)
    expect((failure as Error).message).not.toContain(nativeMessage)
    expect((failure as Error).message).not.toContain('must-not-leak-secret')
  },
)

test('native HTTP errors preserve safe server code without leaking response detail', async () => {
  const native = nativeClient(async () =>
    nativeResponse(
      {
        error: 'invalid_pairing',
        detail: 'secret=must-not-surface',
      },
      400,
    ),
  )
  const transport = new PairingTransport(vi.fn(), native, () => true)

  await expect(
    transport.exchange(
      'https://terminal.example',
      'another-secret',
      'public-key',
      'Android console',
    ),
  ).rejects.toMatchObject({
    status: 400,
    code: 'invalid_pairing',
    retryable: false,
    message:
      'The pairing link is invalid, expired, or already used. Generate a new pairing link. Diagnostic: invalid_pairing.',
  })
})

test('untrusted server error identifiers collapse to a stable HTTP diagnostic', async () => {
  const native = nativeClient(async () =>
    nativeResponse({ error: 'bad value with secret=abc' }, 503),
  )
  const transport = new PairingTransport(vi.fn(), native, () => true)

  await expect(
    transport.refresh('https://terminal.example', 'client-test', 'refresh-secret'),
  ).rejects.toMatchObject({
    status: 503,
    code: 'http_503',
    retryable: true,
    message: 'Authentication failed with HTTP 503. Diagnostic: http_503.',
  })
})
