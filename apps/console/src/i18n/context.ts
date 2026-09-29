import { createContext } from 'react'

import type { Locale, MessageKey } from './catalogs'

export type I18nValue = {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: MessageKey) => string
  number: (value: number, options?: Intl.NumberFormatOptions) => string
  dateTime: (
    value: Date | number | string,
    options?: Intl.DateTimeFormatOptions,
  ) => string
  plural: (count: number) => Intl.LDMLPluralRule
}

export const I18nContext = createContext<I18nValue | null>(null)
