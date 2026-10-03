import type { CSSProperties } from 'react'

import activityIcon from '../assets/icons/tabler/activity.svg'
import alertCircleIcon from '../assets/icons/tabler/alert-circle.svg'
import alertTriangleIcon from '../assets/icons/tabler/alert-triangle.svg'
import arrowLeftIcon from '../assets/icons/tabler/arrow-left.svg'
import arrowsExchangeIcon from '../assets/icons/tabler/arrows-exchange.svg'
import bracesIcon from '../assets/icons/tabler/braces.svg'
import checkIcon from '../assets/icons/tabler/check.svg'
import chevronDownIcon from '../assets/icons/tabler/chevron-down.svg'
import chevronRightIcon from '../assets/icons/tabler/chevron-right.svg'
import chevronUpIcon from '../assets/icons/tabler/chevron-up.svg'
import circleCheckIcon from '../assets/icons/tabler/circle-check.svg'
import copyIcon from '../assets/icons/tabler/copy.svg'
import deviceFloppyIcon from '../assets/icons/tabler/device-floppy.svg'
import dotsIcon from '../assets/icons/tabler/dots.svg'
import externalLinkIcon from '../assets/icons/tabler/external-link.svg'
import eyeIcon from '../assets/icons/tabler/eye.svg'
import heartRateMonitorIcon from '../assets/icons/tabler/heart-rate-monitor.svg'
import infoCircleIcon from '../assets/icons/tabler/info-circle.svg'
import keyIcon from '../assets/icons/tabler/key.svg'
import listCheckIcon from '../assets/icons/tabler/list-check.svg'
import loaderIcon from '../assets/icons/tabler/loader.svg'
import menuIcon from '../assets/icons/tabler/menu-2.svg'
import pencilIcon from '../assets/icons/tabler/pencil.svg'
import playerPauseIcon from '../assets/icons/tabler/player-pause.svg'
import playerPlayIcon from '../assets/icons/tabler/player-play.svg'
import playerStopIcon from '../assets/icons/tabler/player-stop.svg'
import plugConnectedIcon from '../assets/icons/tabler/plug-connected.svg'
import plugOffIcon from '../assets/icons/tabler/plug-off.svg'
import plusIcon from '../assets/icons/tabler/plus.svg'
import refreshIcon from '../assets/icons/tabler/refresh.svg'
import restoreIcon from '../assets/icons/tabler/restore.svg'
import rotateIcon from '../assets/icons/tabler/rotate.svg'
import server2Icon from '../assets/icons/tabler/server-2.svg'
import serverIcon from '../assets/icons/tabler/server.svg'
import settingsIcon from '../assets/icons/tabler/settings.svg'
import shieldCheckIcon from '../assets/icons/tabler/shield-check.svg'
import squareKeyIcon from '../assets/icons/tabler/square-key.svg'
import topologyRingIcon from '../assets/icons/tabler/topology-ring-3.svg'
import trashIcon from '../assets/icons/tabler/trash.svg'
import unlinkIcon from '../assets/icons/tabler/unlink.svg'
import usersIcon from '../assets/icons/tabler/users.svg'
import xIcon from '../assets/icons/tabler/x.svg'

const icons = {
  // Stable semantic entity/navigation names.
  fleet: server2Icon,
  slots: squareKeyIcon,
  connections: plugConnectedIcon,
  settings: settingsIcon,
  server: serverIcon,
  mesh: topologyRingIcon,
  agents: usersIcon,
  tasks: listCheckIcon,
  activity: activityIcon,
  context: bracesIcon,
  health: heartRateMonitorIcon,

  // Stable semantic action names for controls and future IconButton.
  save: deviceFloppyIcon,
  apply: checkIcon,
  reset: restoreIcon,
  cancel: xIcon,
  close: xIcon,
  create: plusIcon,
  edit: pencilIcon,
  delete: trashIcon,
  retry: refreshIcon,
  copy: copyIcon,
  play: playerPlayIcon,
  pause: playerPauseIcon,
  stop: playerStopIcon,
  rotate: rotateIcon,
  connect: plugConnectedIcon,
  disconnect: plugOffIcon,
  release: unlinkIcon,
  reassign: arrowsExchangeIcon,
  details: eyeIcon,
  back: arrowLeftIcon,
  more: dotsIcon,
  menu: menuIcon,
  access: keyIcon,
  secure: shieldCheckIcon,
  'open-external': externalLinkIcon,

  // State/disclosure names used directly by current surfaces.
  loading: loaderIcon,
  success: circleCheckIcon,
  warning: alertTriangleIcon,
  error: alertCircleIcon,
  info: infoCircleIcon,
  'chevron-down': chevronDownIcon,
  'chevron-right': chevronRightIcon,
  'chevron-up': chevronUpIcon,

  // Compatibility aliases for existing call sites. New code should prefer
  // the semantic names above unless the raw visual is itself the meaning.
  'alert-circle': alertCircleIcon,
  'alert-triangle': alertTriangleIcon,
  'arrow-left': arrowLeftIcon,
  braces: bracesIcon,
  'circle-check': circleCheckIcon,
  dots: dotsIcon,
  'heart-rate-monitor': heartRateMonitorIcon,
  'info-circle': infoCircleIcon,
  'list-check': listCheckIcon,
  loader: loaderIcon,
  'menu-2': menuIcon,
  'plug-connected': plugConnectedIcon,
  'server-2': server2Icon,
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
      style={{ '--ui-icon-url': 'url("' + icons[name] + '")' } as CSSProperties}
    />
  )
}
