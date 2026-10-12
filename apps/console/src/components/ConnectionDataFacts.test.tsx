import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { fixtureFleetModel } from '../fixtures/fleet'
import { meshControl } from '../slots/testFixtures'
import { ConnectionDataFacts } from './ConnectionDataFacts'

afterEach(cleanup)
test('reachable server, missing resources, stale Production topology and disabled configuration are separate facts', () => {
  const server = { ...fixtureFleetModel.servers[0], connectionState: 'live' as const, freshness: 'stale' as const,
    resources: { status: 'unavailable' as const, cpu: { status: 'unavailable' as const }, memory: { status: 'unavailable' as const },
      filesystem: { status: 'unavailable' as const }, uptime: { status: 'unavailable' as const } } }
  render(<I18nProvider><ConnectionDataFacts server={server} control={{ control: meshControl(), freshness: 'stale', error: 'request_timeout' }} canConfigure={false} /></I18nProvider>)
  expect(screen.getByText(`${en['connections.serverAvailability']}: ${en['connections.lifecycle.live']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.resourceFreshness']}: ${en['common.unavailable']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.controlState']}: ${en['status.stale']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.configurationReady']}: ${en['connections.notReady']}`)).toBeInTheDocument()
})
test('fresh resources do not imply a readable Mesh or permission to edit configuration', () => {
  const server = { ...fixtureFleetModel.servers[0], freshness: 'fresh' as const,
    resources: { status: 'available' as const, cpu: { status: 'available' as const, usagePercent: 20 }, memory: { status: 'available' as const },
      filesystem: { status: 'available' as const }, uptime: { status: 'available' as const } } }
  render(<I18nProvider><ConnectionDataFacts server={server} control={{ freshness: 'unknown', error: 'forbidden' }} canConfigure={false} /></I18nProvider>)
  expect(screen.getByText(`${en['connections.resourceFreshness']}: ${en['status.fresh']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.controlState']}: ${en['common.unavailable']}`)).toBeInTheDocument()
  expect(screen.getByText(`${en['connections.configurationReady']}: ${en['connections.notReady']}`)).toBeInTheDocument()
})
