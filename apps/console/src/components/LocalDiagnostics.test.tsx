import { cleanup,render,screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach,expect,test } from 'vitest'

import { BrowserDiagnosticJournal } from '../diagnostics/journal'
import { fixtureFleetModel } from '../fixtures/fleet'
import { I18nProvider } from '../i18n/I18nProvider'
import { LocalDiagnostics } from './LocalDiagnostics'

afterEach(() => { cleanup(); localStorage.clear() })

test('Clear log empties the current server diagnostics panel', async () => {
  const server = fixtureFleetModel.servers.find((item) => item.instanceId === 'server-c')!
  const diagnostics = new BrowserDiagnosticJournal(localStorage, () => new Date('2026-10-17T06:00:00Z'))
  diagnostics.append({ type: 'connection_state', instanceId: server.instanceId, server: server.displayName, status: 'reconnecting', code: 'dns_error' })
  render(<I18nProvider><LocalDiagnostics server={server} diagnostics={diagnostics} /></I18nProvider>)

  await userEvent.click(screen.getByRole('button', { name: 'Clear log' }))

  expect(screen.getByText('No diagnostic events yet.')).toBeInTheDocument()
  expect(diagnostics.list()).toEqual([])
})
