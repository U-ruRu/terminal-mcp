import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from 'react'

type ButtonVariant = 'primary' | 'secondary' | 'quiet' | 'destructive'

export function UiButton({
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return <button className={['ui-button', 'ui-button-' + variant, className].filter(Boolean).join(' ')} {...props} />
}

export function Field({
  label,
  hint,
  error,
  children,
  className = '',
}: PropsWithChildren<{ label: ReactNode; hint?: ReactNode; error?: ReactNode; className?: string }>) {
  return (
    <label className={['ui-field', error ? 'ui-field-error' : '', className].filter(Boolean).join(' ')}>
      <span className="ui-field-label">{label}</span>
      {children}
      {error ? <span className="ui-field-message" role="alert">{error}</span> : hint ? <span className="ui-field-message">{hint}</span> : null}
    </label>
  )
}

export type SegmentOption = {
  value: string
  label: ReactNode
  disabled?: boolean
}

export function SegmentedControl({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: SegmentOption[]
  onChange: (value: string) => void
}) {
  return (
    <div className="segmented-control" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          disabled={option.disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export type FeedbackVariant = 'empty' | 'loading' | 'error' | 'partial' | 'destructive'

const feedbackIcon: Record<FeedbackVariant, string> = {
  empty: '○',
  loading: '…',
  error: '!',
  partial: '◐',
  destructive: '!',
}

export function FeedbackState({
  variant,
  title,
  detail,
  action,
}: {
  variant: FeedbackVariant
  title: ReactNode
  detail?: ReactNode
  action?: ReactNode
}) {
  const role = variant === 'error' || variant === 'destructive' ? 'alert' : 'status'
  return (
    <div className={'feedback-state feedback-state-' + variant} role={role}>
      <span className="feedback-state-icon" aria-hidden="true">{feedbackIcon[variant]}</span>
      <div className="feedback-state-copy">
        <strong>{title}</strong>
        {detail ? <p>{detail}</p> : null}
      </div>
      {action ? <div className="feedback-state-action">{action}</div> : null}
    </div>
  )
}

export function Tabs({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: SegmentOption[]
  onChange: (value: string) => void
}) {
  return (
    <div className="ui-tabs" role="tablist" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="tab"
          aria-selected={value === option.value}
          disabled={option.disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
