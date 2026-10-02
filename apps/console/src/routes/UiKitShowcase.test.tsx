import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { UiKitShowcase } from './UiKitShowcase'

test('renders the sanitized M4 component showcase', () => {
  render(<UiKitShowcase />)
  expect(screen.getByRole('heading', { name: 'Component showcase' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Primary' })).toBeInTheDocument()
  expect(screen.getByRole('group', { name: 'Server filter' })).toBeInTheDocument()
  expect(screen.getByRole('tablist', { name: 'Server sections' })).toBeInTheDocument()
  expect(screen.getByText('Partial data')).toBeInTheDocument()
  expect(screen.getByText('Delete slot?')).toBeInTheDocument()
})
