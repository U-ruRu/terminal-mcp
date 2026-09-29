import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')
const allowedRawText = new Set(['Terminal MCP'])

function productionTsxFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return productionTsxFiles(path)
    if (!entry.name.endsWith('.tsx') || entry.name.endsWith('.test.tsx')) return []
    return [path]
  })
}

const violations = []
const childText = /<([A-Za-z][\w.]*)\b[^>]*>\s*([A-Za-z][^<>{}\n]*?)\s*<\/\1>/g
const literalAttribute = /\b(aria-label|title|placeholder|alt)=["']([^"']*[A-Za-z][^"']*)["']/g

for (const path of productionTsxFiles(srcRoot)) {
  const source = readFileSync(path, 'utf8')
  const name = relative(srcRoot, path)
  for (const match of source.matchAll(childText)) {
    const text = match[2].trim()
    if (text && !allowedRawText.has(text)) violations.push(`${name}: raw JSX text: ${text}`)
  }
  for (const match of source.matchAll(literalAttribute)) {
    violations.push(`${name}: raw ${match[1]}: ${match[2]}`)
  }
}

if (violations.length) {
  console.error('Raw user-facing strings detected; add localization keys instead:')
  for (const violation of violations) console.error(`- ${violation}`)
  process.exit(1)
}
console.log('i18n raw UI guard passed')
