import { cleanup,render,screen,waitFor,within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach,expect,test } from 'vitest'

import { App } from './App'
import { BrowserDiagnosticJournal } from './diagnostics/journal'
import { fixtureFleetModel } from './fixtures/fleet'
import { I18nProvider } from './i18n/I18nProvider'
import { ThemeProvider } from './theme/ThemeProvider'

afterEach(() => { cleanup(); localStorage.clear() })

function renderApp(path = '/', diagnostics?: BrowserDiagnosticJournal) {
  return render(
    <I18nProvider>
      <ThemeProvider>
        <MemoryRouter initialEntries={[path]}>
          <App model={fixtureFleetModel} instances={[]} diagnostics={diagnostics} />
        </MemoryRouter>
      </ThemeProvider>
    </I18nProvider>,
  )
}

test('requires explicit server selection for server-scoped global routes', async () => {
  renderApp('/tasks')
  expect(screen.getByRole('heading', { name: 'Choose a server for Tasks' })).toBeInTheDocument()
  expect(screen.getByText(/Console will not pick one for you/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Server C/ })).toHaveAttribute('href', '/servers/server-c/tasks')
})

test('opens the selected server workspace from Fleet', async () => {
  renderApp()
  const serverCard = screen.getByRole('article', { name: 'Server C server' })

  await userEvent.click(within(serverCard).getByRole('link', { name: 'Server C · Live' }))

  expect(document.querySelector('.app-bar-title')).toHaveTextContent('Server C')
  expect(screen.getByText('https://server-c.example.invalid')).toBeInTheDocument()
})

test('Escape closes the navigation drawer and restores its trigger focus', async () => {
  renderApp()
  const toggle = screen.getByRole('button', { name: 'Open navigation' })
  await userEvent.click(toggle)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close navigation' })).toHaveFocus())

  await userEvent.keyboard('{Escape}')

  expect(toggle).toHaveAttribute('aria-expanded', 'false')
  expect(toggle).toHaveFocus()
})
