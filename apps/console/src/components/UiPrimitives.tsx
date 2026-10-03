import { forwardRef, useId, useState, type ButtonHTMLAttributes, type HTMLAttributes, type PropsWithChildren, type ReactNode } from 'react'

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
  const tooltipId = useId()
  const [tooltipVisible, setTooltipVisible] = useState(false)
  const { style, onFocus, onBlur, onMouseEnter, onMouseLeave, ...buttonProps } = props
  const describedBy = tooltipVisible
    ? [buttonProps['aria-describedby'], tooltipId].filter(Boolean).join(' ')
    : buttonProps['aria-describedby']
  return (
    <button
      {...buttonProps}
      ref={ref}
      type={type}
      className={['ui-button', 'ui-button-' + variant, 'ui-icon-button', className].filter(Boolean).join(' ')}
      style={{ minWidth: 44, minHeight: 44, ...style }}
      aria-label={label}
      aria-describedby={describedBy}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      onFocus={(event) => { setTooltipVisible(true); onFocus?.(event) }}
      onBlur={(event) => { setTooltipVisible(false); onBlur?.(event) }}
      onMouseEnter={(event) => { setTooltipVisible(true); onMouseEnter?.(event) }}
      onMouseLeave={(event) => { setTooltipVisible(false); onMouseLeave?.(event) }}
    >
      <Icon name={busy ? 'loading' : icon} />
      {tooltipVisible ? (
        <span id={tooltipId} className="ui-icon-button-tooltip" role="tooltip">
          {label}
        </span>
      ) : null}
    </button>
  )
})

export function IconButtonRow({
  className = '',
  style,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      {...props}
      className={['ui-icon-button-row', className].filter(Boolean).join(' ')}
      style={{ display: 'flex', flexFlow: 'row nowrap', alignItems: 'center', gap: 'var(--space-2)', ...style }}
    />
  )
}

export type ConfirmationDialogProps = {
  title: string
  detail: ReactNode
  cancelLabel: string
  confirmLabel: string
  confirmIcon: IconName
  onCancel: () => void
  onConfirm: () => void
  subject?: ReactNode
  confirmVariant?: Extract<ButtonVariant, 'primary' | 'destructive'>
  busy?: boolean
}

export function ConfirmationDialog({
  title,
  detail,
  cancelLabel,
  confirmLabel,
  confirmIcon,
  onCancel,
  onConfirm,
  subject,
  confirmVariant = 'destructive',
  busy = false,
}: ConfirmationDialogProps) {
  return (
    <div className="floating-status-stack confirmation-surface">
      <div className="panel ui-confirmation-dialog" role="dialog" aria-modal="true" aria-label={title}>
        {subject ? <p><strong>{subject}</strong></p> : null}
        <p>{detail}</p>
        <IconButtonRow className="ui-confirmation-actions">
          <IconButton icon="cancel" variant="secondary" label={cancelLabel} onClick={onCancel} disabled={busy} />
          <UiButton type="button" variant={confirmVariant} onClick={onConfirm} disabled={busy}>
            <Icon name={busy ? 'loading' : confirmIcon} />
            {confirmLabel}
          </UiButton>
        </IconButtonRow>
      </div>
    </div>
  )
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

const feedbackIcon: Record<FeedbackVariant, IconName> = {
  empty: 'info',
  loading: 'loading',
  error: 'error',
  partial: 'warning',
  destructive: 'error',
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
