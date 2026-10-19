import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { ConsoleHttpError } from '../api/client'
import type { NodeRuntimeOperation, NodeRuntimeStatus } from '../api/models'
import { I18nProvider } from '../i18n/I18nProvider'
import { en } from '../i18n/catalogs'
import { NodeRuntimePanel } from './NodeRuntimePanel'

const operation: NodeRuntimeOperation = {
  request_id: 'existing-runtime-request', target: 'terminal', status: 'succeeded',
  created_at: 1, expires_at: 181, error: null,
}
const status: NodeRuntimeStatus = {
  platform: 'windows', supported: true, restart_targets: ['terminal', 'tunnel'],
  services: { terminal: { state: 'running', pid: 123 }, tunnel: { state: 'running', pid: 456 } },
  operation: null,
}
afterEach(() => { cleanup(); localStorage.clear() })

test('a paired read-only profile can observe Windows services with restart disabled', async () => {
  const api = { nodeRuntime: vi.fn().mockResolvedValue(status) }
  render(<I18nProvider><NodeRuntimePanel instanceId="pc" deviceId="phone" enabled canRestart={false} client={() => api} /></I18nProvider>)
  expect(await screen.findByText(en['runtime.title'])).toBeInTheDocument()
  expect(screen.getByRole('button', { name: en['runtime.restart'] + ' · Terminal MCP' })).toBeDisabled()
})

test('an interrupted reply is reconciled by request ID without repeating the restart', async () => {
  const restart = vi.fn().mockRejectedValue(new ConsoleHttpError(0, 'network_error'))
  const api = { nodeRuntime: vi.fn().mockResolvedValue(status), restartNodeRuntime: restart,
    nodeRuntimeOperation: vi.fn().mockImplementation(async (id: string) => ({ ...operation, request_id: id })) }
  render(<I18nProvider><NodeRuntimePanel instanceId="pc" deviceId="phone" enabled canRestart client={() => api} /></I18nProvider>)
  fireEvent.click(await screen.findByRole('button', { name: en['runtime.restart'] + ' · Terminal MCP' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: en['runtime.restart'] }))
  await waitFor(() => expect(api.nodeRuntimeOperation).toHaveBeenCalled())
  expect(restart).toHaveBeenCalledTimes(1)
  const id = restart.mock.calls[0][1]
  expect(api.nodeRuntimeOperation).toHaveBeenCalledWith(id)
})

test('a saved maintenance operation is observed after application restart', async () => {
  localStorage.setItem('tmcp.runtime.pc.phone', JSON.stringify({ requestId: operation.request_id, startedAt: Date.now() }))
  const api = { nodeRuntime: vi.fn().mockResolvedValue(status), restartNodeRuntime: vi.fn(),
    nodeRuntimeOperation: vi.fn().mockResolvedValue(operation) }
  render(<I18nProvider><NodeRuntimePanel instanceId="pc" deviceId="phone" enabled canRestart client={() => api} /></I18nProvider>)
  expect(await screen.findByText(en['runtime.succeeded'])).toBeInTheDocument()
  expect(localStorage.getItem('tmcp.runtime.pc.phone')).toBeNull()
  expect(api.restartNodeRuntime).not.toHaveBeenCalled()
})
