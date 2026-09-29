import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const version = JSON.parse(await readFile(resolve(root, 'android/version.json'), 'utf8'))

if (
  typeof version.versionName !== 'string' ||
  !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version.versionName)
) {
  throw new Error('android/version.json versionName must be semver-like')
}
if (!Number.isInteger(version.versionCode) || version.versionCode < 1) {
  throw new Error('android/version.json versionCode must be a positive integer')
}
console.log(version.versionName + ' (' + version.versionCode + ')')
