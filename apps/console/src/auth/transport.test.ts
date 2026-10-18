import type { HttpOptions,HttpResponse } from '@capacitor/core'
import { expect,test,vi } from 'vitest'

import { PairingTransport,type NativeHttpLike } from './transport'

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
      code: 'one-time-secret',
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
