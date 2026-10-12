import { useI18n } from '../i18n/useI18n'

import type { SlotPolicyInheritanceReadModel } from '../api/models'

export type PolicyInheritanceDisplay = SlotPolicyInheritanceReadModel

const parentLabels = { mesh: 'slots.inheritance.mesh', standalone: 'slots.inheritance.standalone', server: 'slots.inheritance.server' } as const

export function PolicyInheritance({ value }: { value?: PolicyInheritanceDisplay }) {
  const { t } = useI18n()
  if (!value) return <p className="muted">{t('slots.inheritance.unreported')}</p>
  const parent = value.parent
  return <div className="slot-policy-source">
    <p className="muted"><strong>{t('slots.inheritance.source')}: </strong>
      {value.mode === 'stored' ? t('slots.inheritance.stored') : value.mode === 'override' ? t('slots.inheritance.override') : t('slots.inheritance.inherit')}
      {' · '}{value.source === 'mesh' ? t('slots.inheritance.mesh') : value.source === 'standalone' ? t('slots.inheritance.standalone') : t('slots.inheritance.slot')}
    </p>
    {parent ? <p className="muted">{t('slots.inheritance.parent')}: {t(parentLabels[parent.source])} · {parent.id} · {t('slots.revision')} {parent.revision ?? t('common.unavailable')}
      {parent.freshness === 'last_known' ? ` · ${t('slots.inheritance.lastKnown')}` : ''}
      {parent.convergence !== 'converged' ? ` · ${t('slots.inheritance.pending')}` : ''}
    </p> : null}
    {value.pending ? <p role="status" className="attention-strip">{t('slots.inheritance.pending')}</p> : null}
  </div>
}
