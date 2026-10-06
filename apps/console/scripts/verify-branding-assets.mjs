import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const root = new URL('../', import.meta.url).pathname.replace(/\/$/, '')

const fail = (message) => {
  console.error(`branding verification failed: ${message}`)
  process.exit(1)
}

const file = (relative) => join(root, relative)
const sha256 = (relative) => createHash('sha256').update(readFileSync(file(relative))).digest('hex')
const pngSize = (relative) => {
  const data = readFileSync(file(relative))
  const signature = '89504e470d0a1a0a'
  if (data.subarray(0, 8).toString('hex') !== signature || data.subarray(12, 16).toString() !== 'IHDR') {
    fail(`${relative} is not a PNG`)
  }
  return [data.readUInt32BE(16), data.readUInt32BE(20)]
}
const expectSize = (relative, width, height) => {
  const [actualWidth, actualHeight] = pngSize(relative)
  if (actualWidth !== width || actualHeight !== height) {
    fail(`${relative} is ${actualWidth}x${actualHeight}, expected ${width}x${height}`)
  }
}
const expectText = (relative, fragment) => {
  const text = readFileSync(file(relative), 'utf8')
  if (!text.includes(fragment)) fail(`${relative} is missing ${JSON.stringify(fragment)}`)
}

const sourceHashes = {
  'branding/adapted-background.png': '0a7f32e5e352505c804631bf218f196800521bbd2ca3fc3dcc3023877a8ff96a',
  'branding/adapted-foreground.png': '30d44cf7b1beb6c9bd1c39abc1e790cfc8275b2b271c9741e62932d2347d14a4',
  'branding/adapted-gplay.png': '24c7b4f79bd04a6f65cb6a716d269727e9016d355ace7f0a27a98978afb6f899',
  'branding/adapted-notifications.png': '8b32699d0a1b29c4ea39ef1687a0a2440029a72bf43420ccebd9d1d96b35e2d0',
  'branding/adapted-splash.png': 'fe21a689c8ec3d523c3a0d710c74dcaec14caf8c25fc399af0357cb9d007da57',
  'branding/adapted-themed.png': 'f1f97c2dcefc7db31e7fd923d734f47fee87a19180592e210ea6814c7cf7999b',
}
for (const [relative, expected] of Object.entries(sourceHashes)) {
  if (!existsSync(file(relative))) fail(`${relative} is missing`)
  if (sha256(relative) !== expected) fail(`${relative} no longer matches the approved source asset`)
}

const densities = [
  ['mdpi', 108, 48, 24],
  ['hdpi', 162, 72, 36],
  ['xhdpi', 216, 96, 48],
  ['xxhdpi', 324, 144, 72],
  ['xxxhdpi', 432, 192, 96],
]
for (const [density, adaptive, legacy, notification] of densities) {
  const mipmap = `android/app/src/main/res/mipmap-${density}`
  expectSize(`${mipmap}/ic_launcher_background.png`, adaptive, adaptive)
  expectSize(`${mipmap}/ic_launcher_foreground.png`, adaptive, adaptive)
  expectSize(`${mipmap}/ic_launcher_monochrome.png`, adaptive, adaptive)
  expectSize(`${mipmap}/ic_launcher.png`, legacy, legacy)
  expectSize(`${mipmap}/ic_launcher_round.png`, legacy, legacy)
  expectSize(`android/app/src/main/res/drawable-${density}/ic_stat_terminal_mcp.png`, notification, notification)
}

const splashes = {
  'android/app/src/main/res/drawable/splash.png': [480, 320],
  'android/app/src/main/res/drawable-land-mdpi/splash.png': [480, 320],
  'android/app/src/main/res/drawable-land-hdpi/splash.png': [800, 480],
  'android/app/src/main/res/drawable-land-xhdpi/splash.png': [1280, 720],
  'android/app/src/main/res/drawable-land-xxhdpi/splash.png': [1600, 960],
  'android/app/src/main/res/drawable-land-xxxhdpi/splash.png': [1920, 1280],
  'android/app/src/main/res/drawable-port-mdpi/splash.png': [320, 480],
  'android/app/src/main/res/drawable-port-hdpi/splash.png': [480, 800],
  'android/app/src/main/res/drawable-port-xhdpi/splash.png': [720, 1280],
  'android/app/src/main/res/drawable-port-xxhdpi/splash.png': [960, 1600],
  'android/app/src/main/res/drawable-port-xxxhdpi/splash.png': [1280, 1920],
}
for (const [relative, [width, height]] of Object.entries(splashes)) expectSize(relative, width, height)

expectSize('public/favicon.png', 512, 512)
expectSize('public/apple-touch-icon.png', 180, 180)
if (sha256('public/favicon.png') !== sourceHashes['branding/adapted-gplay.png']) {
  fail('favicon.png must remain the approved Google Play artwork')
}

for (const oldResource of [
  'android/app/src/main/res/drawable-v24/ic_launcher_foreground.xml',
  'android/app/src/main/res/drawable/ic_launcher_background.xml',
  'android/app/src/main/res/values/ic_launcher_background.xml',
]) {
  if (existsSync(file(oldResource))) fail(`stale default launcher resource remains: ${oldResource}`)
}

for (const icon of ['ic_launcher.xml', 'ic_launcher_round.xml']) {
  expectText(`android/app/src/main/res/mipmap-anydpi-v26/${icon}`, '@mipmap/ic_launcher_background')
  expectText(`android/app/src/main/res/mipmap-anydpi-v26/${icon}`, '@mipmap/ic_launcher_foreground')
  expectText(`android/app/src/main/res/mipmap-anydpi-v33/${icon}`, '@mipmap/ic_launcher_monochrome')
}
expectText('index.html', 'href="/favicon.png"')
expectText('index.html', 'href="/apple-touch-icon.png"')
expectText('android/app/src/main/AndroidManifest.xml', 'android:icon="@mipmap/ic_launcher"')
expectText('android/app/src/main/AndroidManifest.xml', 'android:roundIcon="@mipmap/ic_launcher_round"')

console.log('branding assets verified')
