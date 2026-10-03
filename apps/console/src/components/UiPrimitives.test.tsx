import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import { FeedbackState, Field, IconButton, SegmentedControl, Tabs, UiButton } from './UiPrimitives'

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
  expect(button).toHaveAttribute('title', 'Save policy')
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
