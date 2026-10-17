import { expect,test,vi } from 'vitest';

import type { RealtimeScheduler } from '../realtime/engine';
import {
FleetVisibilityController,
type FleetVisibilitySource,
} from './policy';

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

function hiddenFleet() {
  const scheduler = new Scheduler()
  const visibility = new Visibility()
  visibility.visibilityState = 'hidden'
  const fleet = {
    startAll: vi.fn(async () => {}),
    recoverAll: vi.fn(async () => {}),
    stopAll: vi.fn(),
  }
  const controller = new FleetVisibilityController(fleet, visibility, scheduler, 30_000)
  controller.start()
  return { scheduler, visibility, fleet, controller }
}

test('background grace expiry suspends the fleet', () => {
  const { scheduler, fleet, controller } = hiddenFleet()

  const elapsed = scheduler.runNext()

  expect(elapsed).toBe(30_000)
  expect(fleet.stopAll).toHaveBeenCalledOnce()
  controller.stop()
})

test('foreground transition restarts a suspended fleet', () => {
  const { scheduler, visibility, fleet, controller } = hiddenFleet()
  scheduler.runNext()

  visibility.set('visible')

  expect(fleet.startAll).toHaveBeenCalledOnce()
  controller.stop()
})

test('foreground transition before suspension cancels grace and recovers the fleet', () => {
  const { scheduler, visibility, fleet, controller } = hiddenFleet()

  visibility.set('visible')

  expect(scheduler.queue).toHaveLength(0)
  expect(fleet.recoverAll).toHaveBeenCalledOnce()
  controller.stop()
})
