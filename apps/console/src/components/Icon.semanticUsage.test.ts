import { describe, expect, it } from 'vitest'

const forbiddenRawIconNames = [
  'server-2',
  'square-key',
  'plug-connected',
  'topology-ring-3',
  'users',
  'list-check',
  'braces',
  'heart-rate-monitor',
  'arrow-left',
  'menu-2',
  'dots',
  'loader',
  'circle-check',
  'alert-triangle',
  'alert-circle',
  'info-circle',
  'x',
] as const

const productionSources = import.meta.glob(
  ['/src/components/**/*.{ts,tsx}', '/src/routes/**/*.{ts,tsx}', '/src/activity/**/*.{ts,tsx}'],
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>

describe('semantic icon API usage', () => {
  it('keeps raw implementation icon names out of production presentation code', () => {
    const violations: string[] = []

    for (const [path, source] of Object.entries(productionSources)) {
      if (path.endsWith('/Icon.tsx') || /\.(?:test|spec)\.[jt]sx?$/.test(path)) continue

      for (const rawName of forbiddenRawIconNames) {
        const literal = new RegExp("(['\"])" + rawName + "\\1")
        if (literal.test(source)) violations.push(path + ': ' + rawName)
      }
    }

    expect(violations).toEqual([])
  })
})
