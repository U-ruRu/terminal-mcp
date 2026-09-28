import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'

import { AppShell } from './AppShell'

afterEach(() => cleanup())

function renderShell(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppShell><div>Page body</div></AppShell>
    </MemoryRouter>,
  )
}

test('keeps one primary navigation surface that can become the mobile bottom bar', () => {
  renderShell('/')
  const navigation = screen.getByRole('navigation', { name: 'Console navigation' })
  expect(within(navigation).getAllByRole('link')).toHaveLength(5)
  expect(within(navigation).getByRole('link', { name: 'Overview' })).toHaveClass('active')
})

test('offers deterministic contextual back navigation for server and task detail routes', () => {
  renderShell('/servers/server-a')
  let context = document.querySelector('.mobile-context')
  expect(context).not.toBeNull()
  expect(within(context as HTMLElement).getByRole('link', { name: 'Go back to fleet' })).toHaveAttribute('href', '/')

  cleanup()
  renderShell('/servers/server-a/tasks/core/T-1')
  context = document.querySelector('.mobile-context')
  expect(within(context as HTMLElement).getByRole('link', { name: 'Go back to tasks' })).toHaveAttribute(
    'href',
    '/servers/server-a/tasks',
  )
})

test('activity scoped to a server exposes a direct return path', async () => {
  renderShell('/activity?server=server-a')
  const context = document.querySelector('.mobile-context') as HTMLElement
  await userEvent.click(within(context).getByRole('link', { name: 'Go back to server' }))
  expect(screen.getByText('Page body')).toBeInTheDocument()
})
