import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import { App } from './App'
import { BrowserDiagnosticJournal } from './diagnostics/journal'
import { fixtureFleetModel } from './fixtures/fleet'
import { I18nProvider } from './i18n/I18nProvider'
import { LOCALE_STORAGE_KEY } from './i18n/runtime'
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

test('renders fixture-backed fleet overview shell', () => {
  renderApp()
  expect(screen.getByRole('button', { name: /servers$/i })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Server C' })).toBeInTheDocument()
  expect(screen.getAllByText('0.10.1')).toHaveLength(2)
  expect(screen.queryByText('Needs attention 2')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Partial / Offline: 2 servers' })).toBeInTheDocument()
})

test('requires explicit server selection for server-scoped global routes', async () => {
  renderApp('/tasks')
  expect(screen.getByRole('heading', { name: 'Choose a server for Tasks' })).toBeInTheDocument()
  expect(screen.getByText(/Console will not pick one for you/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Server C/ })).toHaveAttribute('href', '/servers/server-c/tasks')
})

test('opens a stable server workspace from fleet dashboard', async () => {
  renderApp()
  const serverCard = screen.getByRole('article', { name: 'Server C server' })
  await userEvent.click(within(serverCard).getByRole('link', { name: 'Server C · Live' }))
  expect(document.querySelector('.app-bar-title')).toHaveTextContent('Server C')
  expect(screen.queryByRole('heading', { name: 'Server C' })).not.toBeInTheDocument()
  expect(screen.getByText('https://server-c.example.invalid')).toBeInTheDocument()
  expect(screen.queryByRole('combobox', { name: 'Switch server' })).not.toBeInTheDocument()
  const applicationNavigation = screen.getByRole('navigation', { name: 'Application navigation' })
  expect(within(applicationNavigation).getByRole('link', { name: 'Agents' })).toHaveAttribute('href', '/servers/server-c/agents')
  expect(within(applicationNavigation).getAllByRole('link', { name: 'Slots' }).some((link) => link.getAttribute('href') === '/servers/server-c/slots')).toBe(true)
  expect(document.querySelector('.server-entity-context .server-slots-link')).not.toBeInTheDocument()
  expect(within(applicationNavigation).getByRole('link', { name: 'Context' })).toHaveAttribute('href', '/servers/server-c/context')
  expect(within(applicationNavigation).queryByRole('link', { name: 'Diagnostics' })).not.toBeInTheDocument()
  expect(document.querySelector('.server-local-navigation')).not.toBeInTheDocument()
})

test('server overview owns the collapsed local diagnostics panel', () => {
  const diagnostics = new BrowserDiagnosticJournal(localStorage)
  diagnostics.append({ type: 'connection_status', instanceId: 'server-c', server: 'Server C', status: 'live' })
  renderApp('/servers/server-c', diagnostics)
  const panel = document.querySelector('.server-overview-diagnostics') as HTMLDetailsElement
  expect(panel).toBeInTheDocument()
  expect(panel).not.toHaveClass('panel')
  expect(panel.open).toBe(false)
  expect(within(panel).getByText(/Local diagnostics · 1/)).toBeInTheDocument()
  const nav = screen.getByRole('navigation', { name: 'Application navigation' })
  expect(within(nav).queryByRole('link', { name: 'Diagnostics' })).not.toBeInTheDocument()
})

test('server overview uses a compact non-duplicated runtime badge strip', () => {
  renderApp('/servers/server-c')
  const serverCard = screen.getByRole('article', { name: 'Server C server' })
  const strip = serverCard.querySelector('.server-state-strip') as HTMLElement
  expect(within(strip).getAllByText('Live')).toHaveLength(1)
  expect(within(strip).getByText('Fresh')).toBeInTheDocument()
  expect(strip.querySelectorAll('.status')).toHaveLength(2)
})

test('server overview exposes the same runtime version and resource observation used by Fleet', () => {
  renderApp('/servers/server-c')
  const serverCard = screen.getByRole('article', { name: 'Server C server' })
  expect(serverCard).toHaveTextContent('Version: 0.10.1')
  expect(serverCard).toHaveTextContent('CPU28%')
  expect(serverCard).toHaveTextContent('RAM45%')
  expect(serverCard).toHaveTextContent('Disk43%')
  expect(serverCard).toHaveTextContent('Fresh')
})

test('direct route keeps stale cached server readable after reload', () => {
  renderApp('/servers/server-b')
  expect(document.querySelector('.app-bar-title')).toHaveTextContent('Server B')
  expect(screen.queryByRole('heading', { name: 'Server B' })).not.toBeInTheDocument()
  expect(screen.queryByText(/last cached snapshot/i)).not.toBeInTheDocument()
  const serverCard = screen.getByRole('article', { name: 'Server B server' })
  const statusStrip = serverCard.querySelector('.server-state-strip') as HTMLElement
  expect(within(statusStrip).getByText('Live')).toBeInTheDocument()
  expect(within(statusStrip).getByText('Stale')).toBeInTheDocument()
  expect(screen.getByText('Review Android release path')).toBeInTheDocument()
})

test('direct route keeps offline server snapshot readable without a layout-shifting connection strip', () => {
  renderApp('/servers/server-a')
  expect(document.querySelector('.app-bar-title')).toHaveTextContent('Server A')
  expect(screen.queryByRole('heading', { name: 'Server A' })).not.toBeInTheDocument()
  expect(screen.queryByText(/last cached snapshot/i)).not.toBeInTheDocument()
  const serverCard = screen.getByRole('article', { name: 'Server A server' })
  const statusStrip = serverCard.querySelector('.server-state-strip') as HTMLElement
  expect(within(statusStrip).getByText('Offline')).toBeInTheDocument()
  expect(within(statusStrip).getByText('Stale')).toBeInTheDocument()
  expect(screen.getByText('No active sessions in the cached snapshot.')).toBeInTheDocument()
})

test('global navigation stays global while current server destinations remain contextual', () => {
  renderApp('/servers/server-c/context')
  const nav = screen.getByRole('navigation', { name: 'Application navigation' })
  expect(within(nav).getByText('Server C')).toBeInTheDocument()
  expect(within(nav).getByRole('link', { name: 'Fleet' })).toHaveAttribute('href', '/')
  expect(within(nav).getByRole('link', { name: 'Connections' })).toHaveAttribute('href', '/connections')
  expect(within(nav).getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings')
  expect(within(nav).getByRole('link', { name: 'Context' })).toHaveAttribute('aria-current', 'page')
  expect(within(nav).getByRole('link', { name: 'Tasks' })).toHaveAttribute('href', '/servers/server-c/tasks')
  expect(within(nav).getByRole('link', { name: 'Activity' })).toHaveAttribute('href', '/activity?server=server-c')
})

test('drawer closes predictably on Escape and browser Back', async () => {
  renderApp()
  const toggle = screen.getByRole('button', { name: 'Open navigation' })
  await userEvent.click(toggle)
  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close navigation' })).toHaveFocus())

  await userEvent.keyboard('{Escape}')
  expect(toggle).toHaveAttribute('aria-expanded', 'false')
  expect(toggle).toHaveFocus()

  await userEvent.click(toggle)
  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  fireEvent.popState(window)
  expect(toggle).toHaveAttribute('aria-expanded', 'false')
})


test('changes and persists the Console language from Settings', async () => {
  renderApp('/settings')
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Language' }), 'ru')
  expect(screen.getByRole('heading', { name: 'Настройки', level: 2 })).toBeInTheDocument()
  expect(screen.getByRole('navigation', { name: 'Навигация приложения' })).toHaveTextContent('Флот')
  expect(localStorage.getItem('terminal-mcp.console.locale')).toBe('ru')
  expect(document.documentElement.lang).toBe('ru')
})


test('applies persisted Russian locale to fleet UI without translating server data', () => {
  localStorage.setItem(LOCALE_STORAGE_KEY, 'ru')
  renderApp('/')
  expect(screen.getByRole('button', { name: /серверов$/i })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Server C' })).toBeInTheDocument()
  const serverCard = screen.getByRole('article', { name: 'Server C сервер' })
  expect(serverCard).toHaveTextContent('0.10.1')
  expect(within(serverCard).getByRole('link', { name: 'Server C · Онлайн' })).toHaveAttribute('href', '/servers/server-c')
})

test('applies persisted Spanish locale to explicit server chooser', () => {
  localStorage.setItem(LOCALE_STORAGE_KEY, 'es')
  renderApp('/tasks')
  expect(screen.getByRole('heading', { name: 'Elige un servidor para Tareas' })).toBeInTheDocument()
  expect(screen.getByText(/la consola no elegirá uno por ti/i)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Server C/ })).toHaveAttribute('href', '/servers/server-c/tasks')
})
