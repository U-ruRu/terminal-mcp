import { useI18n } from '../i18n/useI18n'
import { useTheme } from './ThemeContext'
import type { ThemeName } from './theme'

const options: Array<{
  value: ThemeName
  labelKey: 'theme.oledDark' | 'theme.neutralDark' | 'theme.softLight'
}> = [
  { value: 'oled-dark', labelKey: 'theme.oledDark' },
  { value: 'neutral-dark', labelKey: 'theme.neutralDark' },
  { value: 'soft-light', labelKey: 'theme.softLight' },
]

export function ThemeSelect() {
  const { t } = useI18n()
  const { theme, setTheme } = useTheme()
  return (
    <label className="theme-select ui-field">
      <span>{t('settings.theme')}</span>
      <select
        aria-label={t('settings.theme')}
        value={theme}
        onChange={(event) => setTheme(event.target.value as ThemeName)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {t(option.labelKey)}
          </option>
        ))}
      </select>
    </label>
  )
}
