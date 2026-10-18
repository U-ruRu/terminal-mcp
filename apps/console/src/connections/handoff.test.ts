import { expect,test,vi } from 'vitest'
import { attachAppUrlSource,PRODUCTION_CONSOLE_ORIGIN } from './handoff'

function pairingLink(server: string, name: string, code: string): string {
  const payload = JSON.stringify({ v: 2, server, name, code })
  const bytes = new TextEncoder().encode(payload)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  const encoded = btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  return `${PRODUCTION_CONSOLE_ORIGIN}/connect#${encoded}`
}

test('native app URL events deliver the pairing link to the registered consumer', async () => {
  let receive: ((event: { url: string }) => void) | undefined
  const source = {
    addListener: vi.fn(async (_event: 'appUrlOpen', listener: (event: { url: string }) => void) => { receive = listener; return { remove: vi.fn() } }),
    getLaunchUrl: vi.fn(async () => undefined),
  }
  const received: string[] = []
  const detach = await attachAppUrlSource(source, (url) => received.push(url))
  const link = pairingLink('https://warm.example', 'Warm', 'synthetic-pairing-secret')

  receive?.({ url: link })

  expect(received).toEqual([link])
  await detach()
})
