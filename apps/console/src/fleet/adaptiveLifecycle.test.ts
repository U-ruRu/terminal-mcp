import { expect, test, vi } from 'vitest'
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

test('adaptive lifecycle owns one sync loop and background cancels it', async () => {
  const timers: Array<() => void> = []
  const scheduler: RealtimeScheduler = {
    setTimeout(callback) { timers.push(callback); return timers.length },
    clearTimeout: vi.fn(),
  }
  const value = runtime()
  const lifecycle = new FleetAdaptiveLifecycle(value as never, () => [], undefined, undefined, scheduler, 1, 2)
  await lifecycle.startAll()
  expect(value.start).toHaveBeenCalledTimes(1)
  expect(timers).toHaveLength(1)
  timers.shift()?.()
  await vi.waitFor(() => expect(value.syncOnce).toHaveBeenCalledTimes(1))
  expect(timers).toHaveLength(1)
  lifecycle.stopAll()
  expect(value.setBackground).toHaveBeenLastCalledWith(true)
})

test('foreground recovery marks network change and reevaluates', async () => {
  const value = runtime()
  const lifecycle = new FleetAdaptiveLifecycle(value as never, () => [])
  await lifecycle.startAll()
  await lifecycle.recoverAll()
  expect(value.networkChanged).toHaveBeenCalledTimes(1)
  expect(value.syncOnce).toHaveBeenCalled()
  expect(value.evaluate).toHaveBeenCalled()
  lifecycle.stopAll()
})
