import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import { App } from './App'
import { fixtureFleetModel } from './fixtures/fleet'

afterEach(() => cleanup())

function renderApp(path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App model={fixtureFleetModel} instances={[]} />
    </MemoryRouter>,
  )
}

test('renders fixture-backed fleet overview shell', () => {
  renderApp()
  expect(screen.getByRole('heading', { name: 'Fleet overview' })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Server C' })).toBeInTheDocument()
  expect(screen.getAllByText('0.10.1')).toHaveLength(2)
  expect(screen.getByText('Local fleet')).toBeInTheDocument()
})

test('routes between scaffolded console views', async () => {
  renderApp()
  await userEvent.click(screen.getByRole('link', { name: 'Tasks' }))
  expect(screen.getByRole('heading', { name: 'Tasks' })).toBeInTheDocument()
  expect(screen.getByText('Tasks read model')).toBeInTheDocument()
})


test('opens a stable server workspace from fleet dashboard', async () => {
  renderApp()
  const secondaryCard = screen.getByRole('article', { name: 'Server C server' })
  await userEvent.click(within(secondaryCard).getByRole('link', { name: 'Open server' }))
  expect(screen.getByRole('heading', { name: 'Server C' })).toBeInTheDocument()
  expect(screen.getByText('https://server-c.example.invalid')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Back to fleet' })).toHaveAttribute('href', '/')
})

test('direct route keeps stale cached server readable after reload', () => {
  renderApp('/servers/server-b')
  expect(screen.getByRole('heading', { name: 'Server B' })).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('last cached snapshot')
  expect(screen.getByText('Review Android release path')).toBeInTheDocument()
})

test('direct route keeps offline server snapshot readable', () => {
  renderApp('/servers/server-a')
  expect(screen.getByRole('heading', { name: 'Server A' })).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('offline')
  expect(screen.getByText('No active sessions in the cached snapshot.')).toBeInTheDocument()
})

test('switches server workspace without returning to fleet', async () => {
  renderApp('/servers/server-b')
  expect(screen.getByRole('heading', { name: 'Server B' })).toBeInTheDocument()
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Switch server' }), 'server-a')
  expect(screen.getByRole('heading', { name: 'Server A' })).toBeInTheDocument()
})
