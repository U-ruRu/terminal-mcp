import { cleanup,render,screen,waitFor,within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach,expect,test,vi } from 'vitest'

import { ConfirmationDialog,IconButton,SegmentedControl,Tabs } from './UiPrimitives'

afterEach(() => cleanup())

test('SegmentedControl reports the selected option', async () => {
  const onChange = vi.fn()
  render(<SegmentedControl label="Filter" value="all" onChange={onChange}
    options={[{ value: 'all', label: 'All' }, { value: 'live', label: 'Live' }]} />)

  await userEvent.click(screen.getByRole('button', { name: 'Live' }))

  expect(onChange).toHaveBeenCalledWith('live')
})

test('Tabs selects and focuses the next tab from the keyboard', async () => {
  const onChange = vi.fn()
  render(<Tabs label="Sections" value="overview" onChange={onChange}
    options={[{ value: 'overview', label: 'Overview' }, { value: 'tasks', label: 'Tasks' }]} />)
  screen.getByRole('tab', { name: 'Overview' }).focus()

  await userEvent.keyboard('{ArrowRight}')

  expect(onChange).toHaveBeenCalledWith('tasks')
  expect(screen.getByRole('tab', { name: 'Tasks' })).toHaveFocus()
})

test('a busy IconButton rejects another action', async () => {
  const onClick = vi.fn()
  render(<IconButton icon="save" label="Save policy" busy onClick={onClick} />)
  const button = screen.getByRole('button', { name: 'Save policy' })

  await userEvent.click(button)

  expect(button).toBeDisabled()
  expect(button).toHaveAttribute('aria-busy', 'true')
  expect(onClick).not.toHaveBeenCalled()
})

test('ConfirmationDialog executes the explicit confirmation', async () => {
  const onConfirm = vi.fn()
  render(<ConfirmationDialog title="Delete mesh" subject="Production"
    detail="This action cannot be undone." cancelLabel="Cancel" confirmLabel="Delete mesh"
    confirmIcon="delete" onCancel={vi.fn()} onConfirm={onConfirm} />)
  const dialog = screen.getByRole('dialog', { name: 'Delete mesh' })

  await userEvent.click(within(dialog).getByRole('button', { name: 'Delete mesh' }))

  expect(onConfirm).toHaveBeenCalledTimes(1)
})

function ModalHarness({ onCancel }: { onCancel: () => void }) {
  const [open, setOpen] = useState(false)
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open confirmation</button>
    {open ? <ConfirmationDialog title="Delete slot" detail="This action cannot be undone."
      cancelLabel="Cancel" confirmLabel="Delete" confirmIcon="delete"
      onCancel={() => { onCancel(); setOpen(false) }} onConfirm={vi.fn()} /> : null}
  </>
}

test('Escape cancels a modal and restores focus to its trigger', async () => {
  const onCancel = vi.fn()
  render(<ModalHarness onCancel={onCancel} />)
  const trigger = screen.getByRole('button', { name: 'Open confirmation' })
  await userEvent.click(trigger)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus())

  await userEvent.keyboard('{Escape}')

  expect(onCancel).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(trigger).toHaveFocus())
})

test('a modal wraps backward keyboard focus to its final action', async () => {
  render(<ConfirmationDialog title="Delete slot" detail="This action cannot be undone."
    cancelLabel="Cancel" confirmLabel="Delete" confirmIcon="delete"
    onCancel={vi.fn()} onConfirm={vi.fn()} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus())

  await userEvent.tab({ shift: true })

  expect(screen.getByRole('button', { name: 'Delete' })).toHaveFocus()
})
