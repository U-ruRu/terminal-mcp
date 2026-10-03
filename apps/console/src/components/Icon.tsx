import type { CSSProperties } from 'react'

import activityIcon from '../assets/icons/tabler/activity.svg'
import alertCircleIcon from '../assets/icons/tabler/alert-circle.svg'
import alertTriangleIcon from '../assets/icons/tabler/alert-triangle.svg'
import arrowLeftIcon from '../assets/icons/tabler/arrow-left.svg'
import bracesIcon from '../assets/icons/tabler/braces.svg'
import chevronDownIcon from '../assets/icons/tabler/chevron-down.svg'
import chevronRightIcon from '../assets/icons/tabler/chevron-right.svg'
import chevronUpIcon from '../assets/icons/tabler/chevron-up.svg'
import circleCheckIcon from '../assets/icons/tabler/circle-check.svg'
import copyIcon from '../assets/icons/tabler/copy.svg'
import dotsIcon from '../assets/icons/tabler/dots.svg'
import heartRateMonitorIcon from '../assets/icons/tabler/heart-rate-monitor.svg'
import infoCircleIcon from '../assets/icons/tabler/info-circle.svg'
import listCheckIcon from '../assets/icons/tabler/list-check.svg'
import loaderIcon from '../assets/icons/tabler/loader.svg'
import menuIcon from '../assets/icons/tabler/menu-2.svg'
import plugConnectedIcon from '../assets/icons/tabler/plug-connected.svg'
import server2Icon from '../assets/icons/tabler/server-2.svg'
import serverIcon from '../assets/icons/tabler/server.svg'
import settingsIcon from '../assets/icons/tabler/settings.svg'
import squareKeyIcon from '../assets/icons/tabler/square-key.svg'
import topologyRingIcon from '../assets/icons/tabler/topology-ring-3.svg'
import usersIcon from '../assets/icons/tabler/users.svg'
import xIcon from '../assets/icons/tabler/x.svg'

const icons = {
  activity: activityIcon,
  'alert-circle': alertCircleIcon,
  'alert-triangle': alertTriangleIcon,
  'arrow-left': arrowLeftIcon,
  braces: bracesIcon,
  'chevron-down': chevronDownIcon,
  'chevron-right': chevronRightIcon,
  'chevron-up': chevronUpIcon,
  'circle-check': circleCheckIcon,
  copy: copyIcon,
  dots: dotsIcon,
  'heart-rate-monitor': heartRateMonitorIcon,
  'info-circle': infoCircleIcon,
  'list-check': listCheckIcon,
  loader: loaderIcon,
  'menu-2': menuIcon,
  'plug-connected': plugConnectedIcon,
  'server-2': server2Icon,
  server: serverIcon,
  settings: settingsIcon,
  'square-key': squareKeyIcon,
  'topology-ring-3': topologyRingIcon,
  users: usersIcon,
  x: xIcon,
} as const

export type IconName = keyof typeof icons

export function Icon({ name, className = '' }: { name: IconName; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={['ui-icon', 'ui-icon-' + name, className].filter(Boolean).join(' ')}
      style={{ '--ui-icon-url': `url("${icons[name]}")` } as CSSProperties}
    />
  )
}
