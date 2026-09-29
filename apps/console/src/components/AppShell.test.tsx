import { cleanup, render, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test } from 'vitest'
import type { FleetServerReadModel } from '../fleet/readModel'
import { I18nProvider } from '../i18n/I18nProvider'
import { AppShell } from './AppShell'
afterEach(() => cleanup())
const servers: FleetServerReadModel[] = []
function renderShell(path: string) {
  return render(<I18nProvider><MemoryRouter initialEntries={[path]}><AppShell servers={servers}><div>Page body</div></AppShell></MemoryRouter></I18nProvider>)
}
test('offers deterministic contextual back navigation for server and task detail routes', () => {
  renderShell('/servers/server-a')
  let context = document.querySelector('.mobile-context') as HTMLElement
  expect(within(context).getByRole('link', { name: 'Go back to fleet' })).toHaveAttribute('href', '/')
  cleanup()
  renderShell('/servers/server-a/tasks/core/T-1')
  context = document.querySelector('.mobile-context') as HTMLElement
  expect(within(context).getByRole('link', { name: 'Go back to tasks' })).toHaveAttribute('href', '/servers/server-a/tasks')
})
test('server-scoped activity exposes a direct return path', () => {
  renderShell('/activity?server=server-a')
  const context = document.querySelector('.mobile-context') as HTMLElement
  expect(within(context).getByRole('link', { name: 'Go back to server' })).toHaveAttribute('href', '/servers/server-a')
})
