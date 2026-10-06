import { readFileSync } from 'node:fs'
import { expect, test } from 'vitest'

const appCss = readFileSync('src/styles/app.css', 'utf8')

test('resource warning accent does not paint the metric value background', () => {
  expect(appCss).toContain('.resource-metric-attention .resource-metric-value { color: var(--color-warning); }')
  expect(appCss).toContain('.resource-metric-attention .resource-progress > span { background: var(--color-warning); }')
  expect(appCss).not.toMatch(/resource-metric-attention \.resource-metric-value[^}]*background:/)
})
