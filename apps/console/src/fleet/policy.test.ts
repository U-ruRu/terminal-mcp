import { expect, test, vi } from 'vitest'

import type { RealtimeScheduler } from '../realtime/engine'
import {
  FleetVisibilityController,
  withRequestTimeout,
  type FleetVisibilitySource,
} from './policy'

class Scheduler implements RealtimeScheduler {
  queue: Array<{ callback: () => void; delayMs: number; handle: object }> = []

  setTimeout(callback: () => void, delayMs: number): unknown {
    const handle = {}
    this.queue.push({ callback, delayMs, handle })
    return handle
  }

  clearTimeout(handle: unknown): void {
    this.queue = this.queue.filter((item) => item.handle !== handle)
  }

  runNext(): number {
    const item = this.queue.shift()
    if (!item) throw new Error('empty_scheduler')
    item.callback()
    return item.delayMs
  }
}

class Visibility implements FleetVisibilitySource {
  visibilityState = 'visible'
  private listeners = new Set<() => void>()

  addEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.listeners.add(listener)
  }

  removeEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.listeners.delete(listener)
  }

  set(state: string): void {
    this.visibilityState = state
    for (const listener of this.listeners) listener()
  }
}

test('timed fetch aborts a stuck request at the configured fleet bound', async () => {
  const scheduler = new Scheduler()
  let signal: AbortSignal | undefined
  const raw = vi.fn(
    async (_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>(() => {
        signal = init?.signal ?? undefined
      }),
  )
  const fetcher = withRequestTimeout(raw, 1200, scheduler)

  const pending = fetcher('https://alpha.example/health')
  expect(scheduler.queue.map((item) => item.delayMs)).toEqual([1200])
  expect(signal?.aborted).toBe(false)

  expect(scheduler.runNext()).toBe(1200)
  await expect(pending).rejects.toThrow('request_timeout')
  expect(signal?.aborted).toBe(true)
  expect(scheduler.queue).toHaveLength(0)
})

test('background policy suspends fleet after grace and resumes once visible', async () => {
  const scheduler = new Scheduler()
  const visibility = new Visibility()
  const fleet = {
    startAll: vi.fn(async () => {}),
    stopAll: vi.fn(),
  }
  const controller = new FleetVisibilityController(fleet, visibility, scheduler, 30_000)

  controller.start()
  visibility.set('hidden')
  expect(scheduler.queue.map((item) => item.delayMs)).toEqual([30_000])
  expect(fleet.stopAll).not.toHaveBeenCalled()

  scheduler.runNext()
  expect(fleet.stopAll).toHaveBeenCalledTimes(1)

  visibility.set('visible')
  await vi.waitFor(() => expect(fleet.startAll).toHaveBeenCalledTimes(1))

  visibility.set('hidden')
  visibility.set('visible')
  expect(scheduler.queue).toHaveLength(0)
  expect(fleet.stopAll).toHaveBeenCalledTimes(1)

  controller.stop()
})
