import type { Locale } from './catalogs'

const durationUnitAliases: Record<Locale, Record<'hour' | 'minute' | 'second', string[]>> = {
  en: { hour: ['hour', 'hours', 'hr', 'hrs', 'h'], minute: ['minute', 'minutes', 'min', 'mins', 'm'], second: ['second', 'seconds', 'sec', 'secs', 's'] },
  ru: { hour: ['час', 'часа', 'часов', 'ч'], minute: ['минута', 'минуты', 'минут', 'мин'], second: ['секунда', 'секунды', 'секунд', 'сек', 'с'] },
  ka: { hour: ['საათი', 'სთ'], minute: ['წუთი', 'წთ'], second: ['წამი', 'წმ'] },
  es: { hour: ['hora', 'horas', 'h'], minute: ['minuto', 'minutos', 'min'], second: ['segundo', 'segundos', 'seg', 's'] },
}

export function formatDuration(seconds: number | undefined, locale: Locale, unavailable = '—'): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return unavailable
  const safe = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const rest = safe % 60
  const parts: string[] = []
  const format = (value: number, unit: 'hour' | 'minute' | 'second') => new Intl.NumberFormat(locale, { style: 'unit', unit, unitDisplay: 'short' }).format(value)
  if (hours) parts.push(format(hours, 'hour'))
  if (minutes) parts.push(format(minutes, 'minute'))
  if (rest || parts.length === 0) parts.push(format(rest, 'second'))
  return parts.join(' ')
}

export function parseDuration(value: string, locale: Locale): number | null {
  const normalized = value.trim().toLocaleLowerCase(locale)
  if (!normalized) return null
  if (/^\d+$/.test(normalized)) return Number(normalized) * 60
  const aliases = new Map<string, number>()
  const addAliases = (source: Locale) => {
    for (const unit of durationUnitAliases[source].hour) aliases.set(unit, 3600)
    for (const unit of durationUnitAliases[source].minute) aliases.set(unit, 60)
    for (const unit of durationUnitAliases[source].second) aliases.set(unit, 1)
  }
  addAliases('en')
  if (locale !== 'en') addAliases(locale)
  const token = /(\d+)\s*([\p{L}.]+)/gu
  let seconds = 0
  let matched = false
  let lastIndex = 0
  let part: RegExpExecArray | null
  while ((part = token.exec(normalized)) !== null) {
    if (normalized.slice(lastIndex, part.index).trim()) return null
    const factor = aliases.get(part[2].replace(/\.$/, ''))
    if (!factor) return null
    seconds += Number(part[1]) * factor
    matched = true
    lastIndex = token.lastIndex
  }
  return matched && !normalized.slice(lastIndex).trim() && seconds > 0 ? seconds : null
}
