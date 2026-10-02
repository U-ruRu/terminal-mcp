import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import { FeedbackState, Field, SegmentedControl, Tabs, UiButton } from './UiPrimitives'

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
  expect(screen.getByText('Failed').closest('[role="alert"]')).toHaveTextContent('Try again')
})
