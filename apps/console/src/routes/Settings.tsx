import { supportedLocales, type Locale } from '../i18n/catalogs'
import { useI18n } from '../i18n/useI18n'
import { ThemeSelect } from '../theme/ThemeSelect'

export function Settings() {
  const { locale, setLocale, t } = useI18n()
  return (
    <section className="stack" aria-labelledby="settings-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{t('settings.eyebrow')}</p>
          <h2 id="settings-title">{t('settings.title')}</h2>
          <p className="muted">{t('settings.description')}</p>
        </div>
      </div>

      <div className="settings-list">
        <section className="settings-row" aria-labelledby="appearance-title">
          <div className="settings-row-copy">
            <h3 id="appearance-title">{t('settings.theme')}</h3>
            <p className="muted">{t('settings.themeHint')}</p>
          </div>
          <ThemeSelect />
        </section>

        <section className="settings-row" aria-labelledby="language-title">
          <div className="settings-row-copy">
            <h3 id="language-title">{t('settings.language')}</h3>
            <p className="muted">{t('settings.languageHint')}</p>
          </div>
          <label className="ui-field settings-control">
            <span className="ui-field-label">{t('settings.language')}</span>
            <select
              aria-label={t('settings.language')}
              value={locale}
              onChange={(event) => setLocale(event.target.value as Locale)}
            >
              {supportedLocales.map((item) => (
                <option key={item} value={item}>
                  {t(
                    ('locale.' + item) as
                      | 'locale.en'
                      | 'locale.ru'
                      | 'locale.ka'
                      | 'locale.es',
                  )}
                </option>
              ))}
            </select>
          </label>
        </section>
      </div>
    </section>
  )
}
