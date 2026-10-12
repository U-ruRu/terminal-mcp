import { useI18n } from '../i18n/useI18n'

import type { SlotPolicyInheritanceReadModel } from '../api/models'

export type PolicyInheritanceDisplay = SlotPolicyInheritanceReadModel

export function PolicyInheritance({ value }: { value?: PolicyInheritanceDisplay }) {
  const { t } = useI18n()
  if (!value) return <p className="muted">{t('slots.inheritance.unreported')}</p>
  const parent = value.parent
  return <div className="slot-policy-source">
    <p className="muted"><strong>{t('slots.inheritance.source')}: </strong>
      {value.mode === 'stored' ? t('slots.inheritance.stored') : value.mode === 'override' ? t('slots.inheritance.override') : t('slots.inheritance.inherit')}
      {' · '}{value.source === 'mesh' ? t('slots.inheritance.mesh') : value.source === 'standalone' ? t('slots.inheritance.standalone') : t('slots.inheritance.slot')}
    </p>
    {parent ? <p className="muted">{t('slots.inheritance.parent')}: {parent.source === 'mesh' ? t('slots.inheritance.mesh') : t('slots.inheritance.standalone')} · {parent.id} · {t('slots.revision')} {parent.revision ?? t('common.unavailable')}
      {parent.freshness === 'last_known' ? ` · ${t('slots.inheritance.lastKnown')}` : ''}
      {parent.convergence !== 'converged' ? ` · ${t('slots.inheritance.pending')}` : ''}
    </p> : null}
    {value.pending ? <p role="status" className="attention-strip">{t('slots.inheritance.pending')}</p> : null}
  </div>
}
