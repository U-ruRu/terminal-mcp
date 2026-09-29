import { Capacitor } from '@capacitor/core'

export const THEME_STORAGE_KEY = 'terminal-mcp.console.theme'
export const DEFAULT_THEME = 'oled-dark' as const

export const THEMES = ['oled-dark', 'neutral-dark', 'soft-light'] as const
export type ThemeName = (typeof THEMES)[number]

const metaColors: Record<ThemeName, string> = {
  'oled-dark': '#000000',
  'neutral-dark': '#15191d',
  'soft-light': '#f4f2ee',
}

export function isThemeName(value: unknown): value is ThemeName {
  return typeof value === 'string' && (THEMES as readonly string[]).includes(value)
}

export function readStoredTheme(storage: Pick<Storage, 'getItem'> | undefined = globalThis.localStorage): ThemeName {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY)
    return isThemeName(value) ? value : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

export function persistTheme(theme: ThemeName, storage: Pick<Storage, 'setItem'> | undefined = globalThis.localStorage) {
  try {
    storage?.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Storage may be unavailable in hardened/private browser contexts; runtime state still applies.
  }
}

export function themeMetaColor(theme: ThemeName): string {
  return metaColors[theme]
}

export function applyThemeToDocument(theme: ThemeName, documentRef: Document = document) {
  const root = documentRef.documentElement
  root.dataset.theme = theme
  root.style.colorScheme = theme === 'soft-light' ? 'light' : 'dark'
  const meta = documentRef.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  if (meta) meta.content = themeMetaColor(theme)
}

export async function syncNativeSystemBars(theme: ThemeName): Promise<void> {
  if (!Capacitor.isNativePlatform()) return
  try {
    const { StatusBar, Style } = await import('@capacitor/status-bar')
    await Promise.all([
      StatusBar.setBackgroundColor({ color: themeMetaColor(theme) }),
      StatusBar.setStyle({ style: theme === 'soft-light' ? Style.Dark : Style.Light }),
    ])
  } catch {
    // Theme remains correct in the WebView even if a device/OS does not expose status-bar controls.
  }
}
