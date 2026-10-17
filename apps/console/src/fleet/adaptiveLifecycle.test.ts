import { expect,test,vi } from 'vitest'
import type { RealtimeScheduler } from '../realtime/engine'
import { FleetAdaptiveLifecycle } from './adaptiveLifecycle'

function runtime() {
  let state: { status: string } | undefined
  return {
    setBackground: vi.fn(),
    networkChanged: vi.fn(),
    getState: vi.fn(() => state),
    start: vi.fn(async () => { state = { status: 'live' }; return true }),
    syncOnce: vi.fn(async () => {}),
    evaluate: vi.fn(async () => {}),
  }
}

function scheduledLifecycle() {
  const queue = new Map<object, () => void>()
  const scheduler: RealtimeScheduler = {
    setTimeout(callback) { const handle = {}; queue.set(handle, callback); return handle },
    clearTimeout(handle) { queue.delete(handle as object) },
  }
  const value = runtime()
  const lifecycle = new FleetAdaptiveLifecycle(value as never, () => [], undefined, undefined, scheduler)
  return { queue, value, lifecycle }
}

test('a scheduled tick syncs once and schedules its next tick', async () => {
  const { queue, value, lifecycle } = scheduledLifecycle()
  await lifecycle.startAll()
  const [handle, callback] = queue.entries().next().value!
  queue.delete(handle)

  callback()
  await Promise.resolve()

  expect(value.syncOnce).toHaveBeenCalledOnce()
  expect(queue.size).toBe(1)
})

test('background suspension cancels the pending sync tick', async () => {
  const { queue, value, lifecycle } = scheduledLifecycle()
  await lifecycle.startAll()

  lifecycle.stopAll()

  expect(queue.size).toBe(0)
  expect(value.setBackground).toHaveBeenLastCalledWith(true)
})
