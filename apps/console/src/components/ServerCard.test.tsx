import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import { fixtureFleetModel } from '../fixtures/fleet'
import { I18nProvider } from '../i18n/I18nProvider'
import { ServerCard } from './ServerCard'

afterEach(() => cleanup())

function renderLargeServerCard(index: number) {
  return render(
    <I18nProvider>
      <MemoryRouter>
        <ServerCard server={fixtureFleetModel.servers[index]} variant="large" interactive={false} />
      </MemoryRouter>
    </I18nProvider>,
  )
}

test('large ServerCard keeps semantic connection colors with neutral badge geometry', () => {
  renderLargeServerCard(0)
  let strip = screen.getByLabelText('Server A runtime state')
  expect(within(strip).getByText('Offline')).toHaveClass('status', 'server-status', 'server-status-offline')

  cleanup()
  renderLargeServerCard(2)
  strip = screen.getByLabelText('Server C runtime state')
  expect(within(strip).getByText('Live')).toHaveClass('status', 'server-status', 'server-status-healthy')
})
