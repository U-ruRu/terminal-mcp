import { readFileSync } from 'node:fs'
import { expect, test } from 'vitest'

const appCss = readFileSync('src/styles/app.css', 'utf8')

test('compact Fleet and Slot overrides do not reserve regression whitespace', () => {
  const fleetBlock = appCss.match(/\.server-card-compact \.server-card-link \{[^}]*\}/)?.[0] ?? ''
  expect(fleetBlock).not.toContain('min-height: 154px')
  expect(fleetBlock).toContain('padding-block: var(--space-3)')
  expect(appCss).toContain('.slots-surface .slot-card {\n  padding: var(--space-2) var(--space-3);\n  gap: var(--space-1);')
})

test('Server Overview badges share equal columns and diagnostics stay flat', () => {
  expect(appCss).toContain('grid-auto-columns: minmax(0, 1fr);')
  expect(appCss).toContain('.server-overview-diagnostics {\n  display: block;')
  expect(appCss).toContain('background: transparent;')
})
