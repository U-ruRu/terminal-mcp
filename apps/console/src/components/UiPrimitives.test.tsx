import { useState } from 'react'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { ConfirmationDialog, FeedbackState, Field, IconButton, IconButtonRow, SegmentedControl, Tabs, UiButton } from './UiPrimitives'

afterEach(() => cleanup())

test('shared controls expose pressed, selected, disabled and error semantics', async () => {
  const segmentChange = vi.fn()
  const tabChange = vi.fn()
  render(
    <>
      <UiButton disabled>Save</UiButton>
      <Field label="Name" error="Required"><input aria-invalid="true" /></Field>
      <SegmentedControl
        label="Filter"
        value="all"
        onChange={segmentChange}
        options={[{ value: 'all', label: 'All' }, { value: 'live', label: 'Live' }]}
      />
      <Tabs
        label="Sections"
        value="overview"
        onChange={tabChange}
        options={[{ value: 'overview', label: 'Overview' }, { value: 'tasks', label: 'Tasks' }]}
      />
      <FeedbackState variant="error" title="Failed" detail="Try again" />
    </>,
  )

  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  expect(screen.getByText('Required')).toHaveAttribute('role', 'alert')
  expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true')
  await userEvent.click(screen.getByRole('button', { name: 'Live' }))
  expect(segmentChange).toHaveBeenCalledWith('live')
  expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true')
  await userEvent.click(screen.getByRole('tab', { name: 'Tasks' }))
  expect(tabChange).toHaveBeenCalledWith('tasks')
  screen.getByRole('tab', { name: 'Overview' }).focus()
  await userEvent.keyboard('{ArrowRight}')
  expect(tabChange).toHaveBeenCalledWith('tasks')
  expect(screen.getByRole('tab', { name: 'Tasks' })).toHaveFocus()
  expect(screen.getByText('Failed').closest('[role="alert"]')).toHaveTextContent('Try again')
})


test('IconButton exposes semantic icon, accessible label, stable busy state and button semantics', async () => {
  const click = vi.fn()
  const { rerender } = render(
    <IconButton
      icon="save"
      label="Save policy"
      variant="primary"
      aria-expanded="false"
      aria-controls="policy-panel"
      onClick={click}
    />,
  )
  const button = screen.getByRole('button', { name: 'Save policy' })
  expect(button).toHaveAttribute('type', 'button')
  expect(button).toHaveStyle({ minWidth: '44px', minHeight: '44px' })
  expect(button.querySelector('.ui-icon-button-tooltip')).toBeNull()
  button.focus()
  expect(button).toHaveFocus()
  await waitFor(() => expect(button.querySelector('.ui-icon-button-tooltip')).not.toBeNull())
  const tooltip = button.querySelector('.ui-icon-button-tooltip') as HTMLElement
  expect(tooltip).toHaveAttribute('role', 'tooltip')
  expect(tooltip).toHaveTextContent('Save policy')
  expect(button).toHaveAttribute('aria-describedby', tooltip.id)
  expect(button).toHaveAttribute('aria-expanded', 'false')
  expect(button).toHaveAttribute('aria-controls', 'policy-panel')
  expect(button.querySelector('.ui-icon-save')).not.toBeNull()
  await userEvent.click(button)
  expect(click).toHaveBeenCalledTimes(1)

  rerender(<IconButton icon="save" label="Save policy" variant="primary" busy onClick={click} />)
  expect(button).toBeDisabled()
  expect(button).toHaveAttribute('aria-busy', 'true')
  expect(button.querySelector('.ui-icon-loading')).not.toBeNull()

  rerender(<IconButton icon="create" label="Create slot" type="submit" variant="primary" />)
  expect(screen.getByRole('button', { name: 'Create slot' })).toHaveAttribute('type', 'submit')
})

test.each(['primary', 'secondary', 'quiet', 'destructive'] as const)('IconButton supports %s variant', (variant) => {
  render(<IconButton icon="copy" label={variant} variant={variant} />)
  expect(screen.getByRole('button', { name: variant })).toHaveClass('ui-icon-button', 'ui-button-' + variant)
})


test('ConfirmationDialog keeps cancel icon-only and confirm action explicit', async () => {
  const onCancel = vi.fn()
  const onConfirm = vi.fn()
  render(
    <ConfirmationDialog
      title="Delete mesh"
      subject="Production"
      detail="This action cannot be undone."
      cancelLabel="Cancel"
      confirmLabel="Delete mesh"
      confirmIcon="delete"
      onCancel={onCancel}
      onConfirm={onConfirm}
    />,
  )
  const dialog = screen.getByRole('dialog', { name: 'Delete mesh' })
  expect(within(dialog).getByText('Production')).toBeInTheDocument()
  expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveClass('ui-icon-button')
  const confirm = within(dialog).getByRole('button', { name: 'Delete mesh' })
  expect(confirm).toHaveClass('ui-button-destructive')
  expect(confirm.querySelector('.ui-icon-delete')).not.toBeNull()
  expect(confirm).toHaveTextContent('Delete mesh')
  await userEvent.click(confirm)
  expect(onConfirm).toHaveBeenCalledTimes(1)
})


test('IconButtonRow is a compact non-wrapping horizontal action group', () => {
  render(
    <IconButtonRow className="connection-actions" data-testid="actions">
      <IconButton icon="retry" label="Retry" />
      <IconButton icon="disconnect" label="Remove" />
    </IconButtonRow>,
  )
  const row = screen.getByTestId('actions')
  expect(row).toHaveClass('ui-icon-button-row', 'connection-actions')
  expect(row).toHaveStyle({ display: 'flex', flexFlow: 'row nowrap', alignItems: 'center' })
  expect(within(row).getAllByRole('button')).toHaveLength(2)
})


test('ConfirmationDialog is a real modal with inert background, focus containment, Escape and focus restoration', async () => {
  const onCancel = vi.fn()
  const onConfirm = vi.fn()

  function Harness() {
    const [open, setOpen] = useState(false)
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>Open confirmation</button>
        <button type="button">Background action</button>
        {open ? (
          <ConfirmationDialog
            title="Delete slot"
            detail="This action cannot be undone."
            cancelLabel="Cancel"
            confirmLabel="Delete"
            confirmIcon="delete"
            onCancel={() => { onCancel(); setOpen(false) }}
            onConfirm={() => { onConfirm(); setOpen(false) }}
          />
        ) : null}
      </>
    )
  }

  const user = userEvent.setup()
  const { container } = render(<Harness />)
  const trigger = screen.getByRole('button', { name: 'Open confirmation' })
  const background = screen.getByRole('button', { name: 'Background action' })

  await user.click(trigger)

  const dialog = screen.getByRole('dialog', { name: 'Delete slot' })
  const layer = dialog.parentElement as HTMLElement
  const cancel = within(dialog).getByRole('button', { name: 'Cancel' })
  const confirm = within(dialog).getByRole('button', { name: 'Delete' })

  expect(layer).toHaveClass('ui-modal-layer')
  expect(layer).not.toHaveClass('floating-status-stack')
  expect(dialog).toHaveAttribute('aria-modal', 'true')
  await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus())
  expect(container).toHaveAttribute('inert')
  expect(container).toHaveAttribute('aria-hidden', 'true')
  expect(document.body.style.overflow).toBe('hidden')

  await user.tab({ shift: true })
  expect(confirm).toHaveFocus()
  await user.tab()
  expect(cancel).toHaveFocus()

  background.focus()
  await waitFor(() => expect(cancel).toHaveFocus())

  await user.keyboard('{Escape}')
  expect(onCancel).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(trigger).toHaveFocus())
  expect(container).not.toHaveAttribute('inert')
  expect(container).not.toHaveAttribute('aria-hidden')
  expect(document.body.style.overflow).toBe('')

  await user.click(trigger)
  const reopened = screen.getByRole('dialog', { name: 'Delete slot' })
  await user.click(within(reopened).getByRole('button', { name: 'Delete' }))
  expect(onConfirm).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(trigger).toHaveFocus())
})
