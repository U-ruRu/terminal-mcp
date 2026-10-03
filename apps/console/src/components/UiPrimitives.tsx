import { forwardRef, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type HTMLAttributes, type PropsWithChildren, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

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

const MODAL_FOCUSABLE = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusableModalElements(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(MODAL_FOCUSABLE))
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
  const layerRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const onCancelRef = useRef(onCancel)

  useEffect(() => {
    onCancelRef.current = onCancel
  }, [onCancel])

  useEffect(() => {
    const layer = layerRef.current
    const dialog = dialogRef.current
    if (!layer || !dialog) return

    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null

    const initialFocus = cancelRef.current && !cancelRef.current.disabled ? cancelRef.current : dialog
    initialFocus.focus({ preventScroll: true })

    const background = Array.from(document.body.children)
      .filter((element): element is HTMLElement => element instanceof HTMLElement && element !== layer)
      .map((element) => ({
        element,
        inert: element.hasAttribute('inert'),
        ariaHidden: element.getAttribute('aria-hidden'),
      }))

    for (const entry of background) {
      entry.element.setAttribute('inert', '')
      entry.element.setAttribute('aria-hidden', 'true')
    }

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusInside = (preferLast = false) => {
      const focusable = focusableModalElements(dialog)
      const target = preferLast ? focusable.at(-1) : focusable[0]
      ;(target ?? dialog).focus({ preventScroll: true })
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onCancelRef.current()
        return
      }
      if (event.key !== 'Tab') return

      const focusable = focusableModalElements(dialog)
      if (focusable.length === 0) {
        event.preventDefault()
        dialog.focus({ preventScroll: true })
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (!dialog.contains(active)) {
        event.preventDefault()
        focusInside(event.shiftKey)
      } else if (event.shiftKey && active === first) {
        event.preventDefault()
        last.focus({ preventScroll: true })
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus({ preventScroll: true })
      }
    }

    const handleFocusIn = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialog.contains(event.target)) focusInside()
    }

    document.addEventListener('keydown', handleKeyDown, true)
    document.addEventListener('focusin', handleFocusIn)

    return () => {
      document.removeEventListener('keydown', handleKeyDown, true)
      document.removeEventListener('focusin', handleFocusIn)
      document.body.style.overflow = previousOverflow

      for (const entry of background) {
        if (!entry.inert) entry.element.removeAttribute('inert')
        if (entry.ariaHidden === null) entry.element.removeAttribute('aria-hidden')
        else entry.element.setAttribute('aria-hidden', entry.ariaHidden)
      }

      const returnFocus = returnFocusRef.current
      if (returnFocus?.isConnected && !returnFocus.hasAttribute('disabled')) {
        returnFocus.focus({ preventScroll: true })
      }
    }
  }, [])

  return createPortal(
    <div ref={layerRef} className="ui-modal-layer confirmation-surface">
      <div
        ref={dialogRef}
        className="panel ui-confirmation-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
      >
        {subject ? <p><strong>{subject}</strong></p> : null}
        <p>{detail}</p>
        <IconButtonRow className="ui-confirmation-actions">
          <IconButton ref={cancelRef} icon="cancel" variant="secondary" label={cancelLabel} onClick={onCancel} disabled={busy} />
          <UiButton type="button" variant={confirmVariant} onClick={onConfirm} disabled={busy}>
            <Icon name={busy ? 'loading' : confirmIcon} />
            {confirmLabel}
          </UiButton>
        </IconButtonRow>
      </div>
    </div>,
    document.body,
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
