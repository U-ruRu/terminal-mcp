export type DurationUnit = 'seconds' | 'minutes' | 'hours'
export type DurationValue = { amount: string; unit: DurationUnit }

export function durationValue(seconds: number): DurationValue {
  if (seconds > 0 && seconds % 3600 === 0) return { amount: String(seconds / 3600), unit: 'hours' }
  if (seconds > 0 && seconds % 60 === 0) return { amount: String(seconds / 60), unit: 'minutes' }
  return { amount: String(seconds), unit: 'seconds' }
}

export function durationSecondsValue(value: DurationValue): number | null {
  if (!/^\d+$/.test(value.amount.trim())) return null
  const amount = Number(value.amount)
  if (!Number.isSafeInteger(amount)) return null
  const multiplier = value.unit === 'hours' ? 3600 : value.unit === 'minutes' ? 60 : 1
  const seconds = amount * multiplier
  return Number.isSafeInteger(seconds) ? seconds : null
}
