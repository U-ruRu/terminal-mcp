import { type ReactNode, useEffect, useMemo, useState } from 'react'

import type { Locale } from './catalogs'
import { I18nContext, type I18nValue } from './context'
import {
  applyDocumentLocale,
  formatDateTime,
  formatNumber,
  initialLocale,
  LOCALE_STORAGE_KEY,
  pluralCategory,
  translate,
} from './runtime'

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(() => initialLocale())

  useEffect(() => {
    applyDocumentLocale(locale)
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale)
  }, [locale])

  const value = useMemo<I18nValue>(() => ({
    locale,
    setLocale,
    t: (key) => translate(locale, key),
    number: (numberValue, options) => formatNumber(locale, numberValue, options),
    dateTime: (dateValue, options) => formatDateTime(locale, dateValue, options),
    plural: (count) => pluralCategory(locale, count),
  }), [locale])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

