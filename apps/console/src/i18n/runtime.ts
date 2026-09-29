import { catalogs, type Locale, type MessageKey, supportedLocales } from './catalogs'

export const DEFAULT_LOCALE: Locale = 'en'
export const LOCALE_STORAGE_KEY = 'terminal-mcp.console.locale'

export function normalizeLocale(value: string | null | undefined): Locale | null {
  if (!value) return null
  const normalized = value.trim().toLowerCase().replace('_', '-').split('-')[0]
  return supportedLocales.includes(normalized as Locale) ? (normalized as Locale) : null
}

export function detectLocale(
  stored: string | null | undefined,
  preferred: readonly string[] = [],
): Locale {
  return normalizeLocale(stored)
    ?? preferred.map(normalizeLocale).find((locale): locale is Locale => locale !== null)
    ?? DEFAULT_LOCALE
}

export function translate(locale: Locale, key: MessageKey): string {
  return catalogs[locale][key] ?? catalogs[DEFAULT_LOCALE][key]
}

export function formatNumber(locale: Locale, value: number, options?: Intl.NumberFormatOptions) {
  return new Intl.NumberFormat(locale, options).format(value)
}

export function formatDateTime(
  locale: Locale,
  value: Date | number | string,
  options: Intl.DateTimeFormatOptions = { dateStyle: 'medium', timeStyle: 'short' },
) {
  const date = value instanceof Date ? value : new Date(value)
  return new Intl.DateTimeFormat(locale, options).format(date)
}

export function pluralCategory(locale: Locale, count: number) {
  return new Intl.PluralRules(locale).select(count)
}

export function initialLocale(): Locale {
  if (typeof window === 'undefined') return DEFAULT_LOCALE
  return detectLocale(
    window.localStorage.getItem(LOCALE_STORAGE_KEY),
    window.navigator.languages.length ? window.navigator.languages : [window.navigator.language],
  )
}

export function applyDocumentLocale(locale: Locale) {
  if (typeof document !== 'undefined') document.documentElement.lang = locale
}
