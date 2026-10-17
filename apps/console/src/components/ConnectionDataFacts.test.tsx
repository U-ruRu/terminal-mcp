import { cleanup,render,screen } from '@testing-library/react'
import { afterEach,expect,test } from 'vitest'
import { fixtureFleetModel } from '../fixtures/fleet'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { ConnectionDataFacts } from './ConnectionDataFacts'

afterEach(cleanup)

test('fresh resources do not imply a readable Mesh or permission to edit configuration', () => {
  const server = { ...fixtureFleetModel.servers[0], freshness: 'fresh' as const,
    resources: { status: 'available' as const, cpu: { status: 'available' as const, usagePercent: 20 }, memory: { status: 'available' as const },
      filesystem: { status: 'available' as const }, uptime: { status: 'available' as const } } }
  render(<I18nProvider><ConnectionDataFacts server={server} control={{ freshness: 'unknown', error: 'forbidden' }} canConfigure={false} /></I18nProvider>)
  expect(screen.getByText(`${en['connections.resourceFreshness']}: ${en['status.fresh']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.controlState']}: ${en['common.unavailable']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.configurationReady']}: ${en['connections.notReady']}`)).toBeInTheDocument()
})
