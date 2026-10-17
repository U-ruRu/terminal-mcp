import { cleanup,render,screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach,expect,test } from 'vitest'

import { I18nProvider } from '../i18n/I18nProvider'
import { LOCALE_STORAGE_KEY } from '../i18n/runtime'
import { ThemeProvider } from '../theme/ThemeProvider'
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

test('selecting the language updates Settings and persists that choice', async () => {
  renderSettings()

  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Language' }), 'ru')

  expect(screen.getByRole('heading', { name: 'Настройки' })).toBeInTheDocument()
  expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('ru')
})
