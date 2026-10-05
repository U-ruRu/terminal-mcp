import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test } from 'vitest'

import { I18nProvider } from '../i18n/I18nProvider'
import { LOCALE_STORAGE_KEY } from '../i18n/runtime'
import { ThemeProvider } from '../theme/ThemeProvider'
import { applyThemeToDocument, readStoredTheme, THEME_STORAGE_KEY } from '../theme/theme'
import { Settings } from './Settings'

function renderSettings() {
  return render(
    <I18nProvider>
      <ThemeProvider>
        <Settings />
      </ThemeProvider>
    </I18nProvider>,
  )
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  document.documentElement.dataset.theme = 'oled-dark'
  document.documentElement.lang = 'en'
})

test('applies and restores localized application preferences from Settings', async () => {
  const first = renderSettings()
  expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()
  expect(document.querySelector('.settings-version')).toHaveTextContent('APK 0.2.29 · code 31')

  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Language' }), 'ru')
  expect(screen.getByRole('heading', { name: 'Настройки' })).toBeInTheDocument()
  expect(document.documentElement.lang).toBe('ru')

  const theme = screen.getByRole('combobox', { name: 'Тема' })
  expect(theme).toHaveTextContent('OLED-тёмная')
  expect(theme).toHaveTextContent('Мягкая светлая')
  await userEvent.selectOptions(theme, 'soft-light')

  expect(document.documentElement.dataset.theme).toBe('soft-light')
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('soft-light')
  expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('ru')

  first.unmount()
  document.documentElement.dataset.theme = 'oled-dark'
  document.documentElement.lang = 'en'
  applyThemeToDocument(readStoredTheme())

  renderSettings()
  expect(screen.getByRole('heading', { name: 'Настройки' })).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'Тема' })).toHaveValue('soft-light')
  expect(document.documentElement.dataset.theme).toBe('soft-light')
  expect(document.documentElement.lang).toBe('ru')
})
