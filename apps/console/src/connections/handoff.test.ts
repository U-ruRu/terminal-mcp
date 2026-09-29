import { expect, test, vi } from 'vitest'
import { attachAppUrlSource, inspectPairingHandoff, PRODUCTION_CONSOLE_ORIGIN } from './handoff'

function pairingLink(server: string, name: string, secret: string): string {
  const payload = JSON.stringify({ v: 1, server, name, secret })
  const bytes = new TextEncoder().encode(payload)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  const encoded = btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  return `${PRODUCTION_CONSOLE_ORIGIN}/connect#${encoded}`
}

test('accepts canonical Base64URL JSON handoff and exposes only safe target identity', () => {
  const value = pairingLink('https://Target.Example', 'Secondary Test', 'one-time-secret-123456789')
  const result = inspectPairingHandoff(value, PRODUCTION_CONSOLE_ORIGIN)
  expect(result).toEqual({ origin: 'https://target.example', name: 'Secondary Test' })
  expect(JSON.stringify(result)).not.toContain('one-time-secret')

  expect(() => inspectPairingHandoff(
    pairingLink('https://target.example', 'Target', 'one-time-secret-123456789').replace(PRODUCTION_CONSOLE_ORIGIN, 'https://evil.example'),
    PRODUCTION_CONSOLE_ORIGIN,
  )).toThrowError('invalid_pairing_link')
  expect(() => inspectPairingHandoff(
    `${PRODUCTION_CONSOLE_ORIGIN}/connect?server=https%3A%2F%2Ftarget.example#old-format-secret`,
    PRODUCTION_CONSOLE_ORIGIN,
  )).toThrowError('invalid_pairing_link')
  expect(() => inspectPairingHandoff(
    'https://target.example/connect#old-direct-format-secret',
    PRODUCTION_CONSOLE_ORIGIN,
  )).toThrowError('invalid_pairing_link')
})

test('delivers cold getLaunchUrl and warm appUrlOpen after listener registration', async () => {
  let warm: ((event: { url: string }) => void) | undefined
  const remove = vi.fn(async () => undefined)
  const received: string[] = []
  const source = {
    addListener: vi.fn(async (_event: 'appUrlOpen', listener: (event: { url: string }) => void) => { warm = listener; return { remove } }),
    getLaunchUrl: vi.fn(async () => ({ url: pairingLink('https://cold.example', 'Cold', 'cold-secret-value-123456789') })),
  }
  const detach = await attachAppUrlSource(source, (url) => received.push(url))
  warm?.({ url: pairingLink('https://warm.example', 'Warm', 'warm-secret-value-123456789') })
  expect(source.addListener.mock.invocationCallOrder[0]).toBeLessThan(source.getLaunchUrl.mock.invocationCallOrder[0])
  expect(received.map((url) => inspectPairingHandoff(url, PRODUCTION_CONSOLE_ORIGIN))).toEqual([
    { origin: 'https://cold.example', name: 'Cold' },
    { origin: 'https://warm.example', name: 'Warm' },
  ])
  await detach(); expect(remove).toHaveBeenCalledOnce()
})
