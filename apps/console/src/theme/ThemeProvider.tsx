import { type ReactNode, useEffect, useMemo, useState } from 'react'

import {
  applyThemeToDocument,
  DEFAULT_THEME,
  isThemeName,
  persistTheme,
  readStoredTheme,
  syncNativeSystemBars,
  type ThemeName,
} from './theme'

import { ThemeContext, type ThemeContextValue } from './ThemeContext'

function initialTheme(): ThemeName {
  if (typeof document !== 'undefined' && isThemeName(document.documentElement.dataset.theme)) {
    return document.documentElement.dataset.theme
  }
  if (typeof window !== 'undefined') return readStoredTheme(window.localStorage)
  return DEFAULT_THEME
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(initialTheme)

  useEffect(() => {
    applyThemeToDocument(theme)
    persistTheme(theme)
    void syncNativeSystemBars(theme)
  }, [theme])

  const value = useMemo<ThemeContextValue>(() => ({ theme, setTheme: setThemeState }), [theme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
