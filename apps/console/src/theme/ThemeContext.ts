import { createContext, useContext } from 'react'

import type { ThemeName } from './theme'

export type ThemeContextValue = {
  theme: ThemeName
  setTheme: (theme: ThemeName) => void
}

export const ThemeContext = createContext<ThemeContextValue | null>(null)

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext)
  if (!value) throw new Error('useTheme must be used within ThemeProvider')
  return value
}
