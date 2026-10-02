import { useState } from 'react'

import { FeedbackState, Field, SegmentedControl, Tabs, UiButton } from '../components/UiPrimitives'

export function UiKitShowcase() {
  const [segment, setSegment] = useState('all')
  const [tab, setTab] = useState('overview')

  return (
    <section className="stack ui-kit-showcase" aria-labelledby="ui-kit-title">
      <div className="page-heading">
        <div>
          <p className="eyebrow">M4 UI Kit</p>
          <h2 id="ui-kit-title">Component showcase</h2>
          <p className="muted">Sanitized visual acceptance surface for shared Console primitives.</p>
        </div>
      </div>

      <article className="panel ui-kit-section">
        <div><p className="eyebrow">Foundation</p><h3>Surfaces and status colors</h3></div>
        <div className="ui-token-row" aria-label="Foundation colors">
          <span className="ui-token ui-token-bg">OLED</span>
          <span className="ui-token ui-token-surface">Surface</span>
          <span className="ui-token ui-token-raised">Raised</span>
          <span className="ui-token ui-token-accent">Action</span>
          <span className="ui-token ui-token-success">Success</span>
          <span className="ui-token ui-token-warning">Warning</span>
          <span className="ui-token ui-token-danger">Critical</span>
          <span className="ui-token ui-token-stale">Stale</span>
        </div>
      </article>

      <article className="panel ui-kit-section">
        <div><p className="eyebrow">Controls</p><h3>Buttons, fields and selection</h3></div>
        <div className="ui-kit-control-row">
          <UiButton>Primary</UiButton>
          <UiButton variant="secondary">Secondary</UiButton>
          <UiButton variant="quiet">Quiet</UiButton>
          <UiButton variant="destructive">Delete</UiButton>
          <UiButton disabled>Disabled</UiButton>
        </div>
        <div className="ui-kit-form-grid">
          <Field label="Server name" hint="Visible to the operator">
            <input defaultValue="Secondary" />
          </Field>
          <Field label="Namespace">
            <select defaultValue="all"><option value="all">All namespaces</option><option value="console">console</option></select>
          </Field>
          <Field label="Invalid field" error="Enter a valid value">
            <input aria-invalid="true" defaultValue="bad value" />
          </Field>
        </div>
        <SegmentedControl
          label="Server filter"
          value={segment}
          onChange={setSegment}
          options={[
            { value: 'all', label: 'All' },
            { value: 'attention', label: 'Needs attention' },
            { value: 'live', label: 'Live' },
          ]}
        />
        <Tabs
          label="Server sections"
          value={tab}
          onChange={setTab}
          options={[
            { value: 'overview', label: 'Overview' },
            { value: 'tasks', label: 'Tasks' },
            { value: 'activity', label: 'Activity' },
          ]}
        />
      </article>

      <article className="panel ui-kit-section">
        <div><p className="eyebrow">Feedback</p><h3>Empty, loading, partial, error and destructive states</h3></div>
        <div className="ui-kit-feedback-grid">
          <FeedbackState variant="empty" title="Nothing here yet" detail="The surface is valid and has no items." />
          <FeedbackState variant="loading" title="Loading" detail="Keeping the last stable layout while data arrives." />
          <FeedbackState variant="partial" title="Partial data" detail="Some live data is unavailable; cached content remains readable." />
          <FeedbackState variant="error" title="Request failed" detail="Retry is safe; existing data is preserved." action={<UiButton variant="secondary">Retry</UiButton>} />
          <FeedbackState variant="destructive" title="Delete slot?" detail="This action removes the persistent slot." action={<UiButton variant="destructive">Delete</UiButton>} />
        </div>
      </article>
    </section>
  )
}
