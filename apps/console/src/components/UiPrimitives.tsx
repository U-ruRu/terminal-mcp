import { forwardRef, type ButtonHTMLAttributes, type PropsWithChildren, type ReactNode } from 'react'

import { Icon, type IconName } from './Icon'

type ButtonVariant = 'primary' | 'secondary' | 'quiet' | 'destructive'

export function UiButton({
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return <button className={['ui-button', 'ui-button-' + variant, className].filter(Boolean).join(' ')} {...props} />
}

export type IconButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'aria-label' | 'title'> & {
  label: string
  icon: IconName
  variant?: ButtonVariant
  busy?: boolean
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({
  label,
  icon,
  variant = 'quiet',
  busy = false,
  disabled = false,
  type = 'button',
  className = '',
  ...props
}, ref) {
  return (
    <button
      {...props}
      ref={ref}
      type={type}
      className={['ui-button', 'ui-button-' + variant, 'ui-icon-button', className].filter(Boolean).join(' ')}
      aria-label={label}
      aria-busy={busy || undefined}
      title={label}
      disabled={disabled || busy}
    >
      <Icon name={busy ? 'loading' : icon} />
    </button>
  )
})

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

const feedbackIcon: Record<FeedbackVariant, IconName> = {
  empty: 'info-circle',
  loading: 'loader',
  error: 'alert-circle',
  partial: 'alert-triangle',
  destructive: 'alert-circle',
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
  const role = variant === 'error' || variant === 'destructive' ? 'alert' : variant === 'loading' || variant === 'partial' ? 'status' : undefined
  return (
    <div className={'feedback-state feedback-state-' + variant} role={role}>
      <span className="feedback-state-icon" aria-hidden="true"><Icon name={feedbackIcon[variant]} /></span>
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
  const enabled = options.filter((option) => !option.disabled)

  const moveFocus = (currentValue: string, direction: 'next' | 'previous' | 'first' | 'last', element: HTMLButtonElement) => {
    if (enabled.length === 0) return
    const currentIndex = Math.max(0, enabled.findIndex((option) => option.value === currentValue))
    const nextIndex = direction === 'first'
      ? 0
      : direction === 'last'
        ? enabled.length - 1
        : direction === 'next'
          ? (currentIndex + 1) % enabled.length
          : (currentIndex - 1 + enabled.length) % enabled.length
    const next = enabled[nextIndex]
    onChange(next.value)
    const container = element.closest('[role="tablist"]')
    const buttons = Array.from(container?.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)') ?? [])
    buttons[nextIndex]?.focus()
  }

  return (
    <div className="ui-tabs" role="tablist" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="tab"
          aria-selected={value === option.value}
          tabIndex={value === option.value ? 0 : -1}
          disabled={option.disabled}
          onClick={() => onChange(option.value)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowRight') {
              event.preventDefault()
              moveFocus(option.value, 'next', event.currentTarget)
            } else if (event.key === 'ArrowLeft') {
              event.preventDefault()
              moveFocus(option.value, 'previous', event.currentTarget)
            } else if (event.key === 'Home') {
              event.preventDefault()
              moveFocus(option.value, 'first', event.currentTarget)
            } else if (event.key === 'End') {
              event.preventDefault()
              moveFocus(option.value, 'last', event.currentTarget)
            }
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
