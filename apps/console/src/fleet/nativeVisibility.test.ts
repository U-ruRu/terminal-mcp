import { expect, test, vi } from 'vitest'

import { NativeFleetVisibilitySource } from './nativeVisibility'

test('native app state maps background and foreground to visibility transitions', async () => {
  let listener: ((state: { isActive: boolean }) => void) | undefined
  const remove = vi.fn(async () => {})
  const app = {
    getState: vi.fn(async () => ({ isActive: true })),
    addListener: vi.fn(async (_name: 'appStateChange', next: (state: { isActive: boolean }) => void) => {
      listener = next
      return { remove }
    }),
  }
  const source = new NativeFleetVisibilitySource(app, 'visible')
  const changed = vi.fn()
  source.addEventListener('visibilitychange', changed)
  await vi.waitFor(() => expect(app.addListener).toHaveBeenCalledTimes(1))
  listener?.({ isActive: false })
  expect(source.visibilityState).toBe('hidden')
  listener?.({ isActive: true })
  expect(source.visibilityState).toBe('visible')
  expect(changed).toHaveBeenCalledTimes(2)
  source.removeEventListener('visibilitychange', changed)
  await vi.waitFor(() => expect(remove).toHaveBeenCalledTimes(1))
})
