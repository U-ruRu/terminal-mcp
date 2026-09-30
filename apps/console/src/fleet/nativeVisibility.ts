import { App as CapacitorApp } from '@capacitor/app'
import { Capacitor } from '@capacitor/core'

import type { FleetVisibilitySource } from './policy'

type NativeAppStateSource = {
  getState(): Promise<{ isActive: boolean }>
  addListener(
    eventName: 'appStateChange',
    listener: (state: { isActive: boolean }) => void,
  ): Promise<{ remove(): Promise<void> }>
}

export class NativeFleetVisibilitySource implements FleetVisibilitySource {
  visibilityState: string
  private readonly listeners = new Set<() => void>()
  private handle: { remove(): Promise<void> } | null = null
  private starting = false
  private revision = 0

  constructor(
    private readonly app: NativeAppStateSource,
    initialState: string = document.visibilityState,
  ) {
    this.visibilityState = initialState === 'hidden' ? 'hidden' : 'visible'
  }

  addEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.listeners.add(listener)
    this.ensureStarted()
  }

  removeEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.listeners.delete(listener)
    if (this.listeners.size === 0) this.stopNativeListener()
  }

  private ensureStarted(): void {
    if (this.handle || this.starting) return
    this.starting = true
    const initialRevision = this.revision
    void this.app.addListener('appStateChange', ({ isActive }) => {
      this.revision += 1
      this.update(isActive)
    }).then((handle) => {
      this.starting = false
      if (this.listeners.size === 0) {
        void handle.remove()
        return
      }
      this.handle = handle
    }).catch(() => { this.starting = false })
    void this.app.getState().then(({ isActive }) => {
      if (this.revision === initialRevision) this.update(isActive)
    }).catch(() => {})
  }

  private stopNativeListener(): void {
    const handle = this.handle
    this.handle = null
    if (handle) void handle.remove()
  }

  private update(isActive: boolean): void {
    const next = isActive ? 'visible' : 'hidden'
    if (next === this.visibilityState) return
    this.visibilityState = next
    for (const listener of this.listeners) listener()
  }
}

export function defaultFleetVisibilitySource(): FleetVisibilitySource {
  if (!Capacitor.isNativePlatform()) return document
  return new NativeFleetVisibilitySource(CapacitorApp)
}
