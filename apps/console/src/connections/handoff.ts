import {
  inspectCanonicalPairingLink,
  PRODUCTION_CONSOLE_ORIGIN,
} from './pairingLink'

export { PRODUCTION_CONSOLE_ORIGIN }

export function inspectPairingHandoff(
  value: string,
  trustedOuterOrigin: string,
): { origin: string; name: string } {
  return inspectCanonicalPairingLink(value, trustedOuterOrigin)
}

export type AppUrlSource = {
  getLaunchUrl: () => Promise<{ url: string } | undefined>
  addListener: (eventName: 'appUrlOpen', listener: (event: { url: string }) => void) => Promise<{ remove: () => Promise<void> | void }>
}

export async function attachAppUrlSource(
  source: AppUrlSource,
  receive: (url: string) => void,
): Promise<() => Promise<void>> {
  const handle = await source.addListener('appUrlOpen', ({ url }) => receive(url))
  const launch = await source.getLaunchUrl()
  if (launch?.url) receive(launch.url)
  return async () => { await handle.remove() }
}
