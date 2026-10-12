import { useI18n } from '../i18n/useI18n'
import type { DurationUnit, DurationValue } from './duration'

export function DurationField({ label, value, disabled, onChange }: {
  label: string
  value: DurationValue
  disabled: boolean
  onChange: (value: DurationValue) => void
}) {
  const { t } = useI18n()
  return <label className="ui-field"><span>{label}</span><span className="duration-control">
    <input aria-label={label} inputMode="numeric" type="number" min="0" step="1" value={value.amount} disabled={disabled} onChange={(event) => onChange({ ...value, amount: event.target.value })} />
    <select aria-label={label + ' · ' + t('slots.unit')} value={value.unit} disabled={disabled} onChange={(event) => onChange({ ...value, unit: event.target.value as DurationUnit })}>
      <option value="seconds">{t('slots.unit.seconds')}</option><option value="minutes">{t('slots.unit.minutes')}</option><option value="hours">{t('slots.unit.hours')}</option>
    </select>
  </span></label>
}
