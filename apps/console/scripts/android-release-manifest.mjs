import { createHash } from 'node:crypto'
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const version = JSON.parse(await readFile(resolve(root, 'android/version.json'), 'utf8'))
const apkPath = resolve(
  root,
  process.env.TERMINAL_MCP_ANDROID_APK_PATH ??
    'android/app/build/outputs/apk/release/app-release.apk',
)
const apkUrl = process.env.TERMINAL_MCP_ANDROID_APK_URL
const cert = (process.env.TERMINAL_MCP_ANDROID_SIGNING_CERT_SHA256 ?? '')
  .replaceAll(':', '')
  .toLowerCase()
const sourceCommitSha = (process.env.TERMINAL_MCP_SOURCE_COMMIT_SHA ?? '').toLowerCase()
const minimumVersionCode = Number(
  process.env.TERMINAL_MCP_ANDROID_MINIMUM_VERSION_CODE ?? version.versionCode,
)

if (!apkUrl || new URL(apkUrl).protocol !== 'https:') {
  throw new Error('TERMINAL_MCP_ANDROID_APK_URL must be an https URL')
}
if (!/^[a-f0-9]{64}$/.test(cert)) {
  throw new Error('TERMINAL_MCP_ANDROID_SIGNING_CERT_SHA256 must be a SHA-256 fingerprint')
}
if (!/^[a-f0-9]{40}$/.test(sourceCommitSha)) {
  throw new Error('TERMINAL_MCP_SOURCE_COMMIT_SHA must be an exact 40-character git SHA')
}
if (
  !Number.isInteger(minimumVersionCode) ||
  minimumVersionCode < 1 ||
  minimumVersionCode > version.versionCode
) {
  throw new Error('TERMINAL_MCP_ANDROID_MINIMUM_VERSION_CODE is invalid')
}

const bytes = await readFile(apkPath)
const apkStat = await stat(apkPath)
const manifest = {
  schemaVersion: 1,
  versionName: version.versionName,
  versionCode: version.versionCode,
  minimumVersionCode,
  apkUrl,
  apkSha256: createHash('sha256').update(bytes).digest('hex'),
  apkSize: apkStat.size,
  signingCertificateSha256: cert,
  sourceCommitSha,
  publishedAt: new Date().toISOString(),
  ...(process.env.TERMINAL_MCP_ANDROID_RELEASE_NOTES
    ? { releaseNotes: process.env.TERMINAL_MCP_ANDROID_RELEASE_NOTES }
    : {}),
}

const outDir = resolve(root, 'dist/android')
const out = resolve(outDir, 'latest.json')
await mkdir(outDir, { recursive: true })
await writeFile(out, JSON.stringify(manifest, null, 2) + '\n')
console.log(out)
