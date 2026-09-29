import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test } from 'vitest'

import { I18nProvider } from '../i18n/I18nProvider'
import { ThemeProvider } from './ThemeProvider'
import { ThemeSelect } from './ThemeSelect'
import { THEME_STORAGE_KEY } from './theme'

afterEach(() => {
  cleanup()
  localStorage.clear()
  document.documentElement.dataset.theme = 'oled-dark'
})

test('lets the user select and persist Soft Light', async () => {
  document.documentElement.dataset.theme = 'oled-dark'
  render(
    <I18nProvider>
      <ThemeProvider>
        <ThemeSelect />
      </ThemeProvider>
    </I18nProvider>,
  )
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Theme' }), 'soft-light')
  expect(document.documentElement.dataset.theme).toBe('soft-light')
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('soft-light')
})
