import type { ActivityEventReadModel } from '../api/models'
import type { Locale } from '../i18n/catalogs'

type Copy = {
  unknownAgent: string
  ready: string
  started: string
  stopping: string
  suspended: string
  ended: string
  broadcast: string
  queued: string
  running: string
  completed: string
  cancelled: string
  failed: string
  online: string
  offline: string
  stale: string
  authRevoked: string
  healthChanged: string
  claimed: string
  released: string
  blocked: string
  done: string
  comment: string
  generic: string
}

const copyByLocale: Record<Locale, Copy> = {
  en: {
    unknownAgent: 'Unknown agent', ready: 'Ready to work', started: 'Started session', stopping: 'Ending session',
    suspended: 'Session paused', ended: 'Finished session', broadcast: 'Everyone', queued: 'Queued',
    running: 'Running…', completed: '✓ Completed', cancelled: 'Cancelled', failed: 'Error',
    online: 'is online again', offline: 'lost connection', stale: 'data is stale', authRevoked: 'authorization revoked',
    healthChanged: 'health changed', claimed: 'Claimed task', released: 'Released task', blocked: 'Blocked',
    done: 'Completed', comment: 'Comment on', generic: 'Activity update',
  },
  ru: {
    unknownAgent: 'Неизвестный агент', ready: 'Готов к работе', started: 'Начал сессию', stopping: 'Завершает сессию',
    suspended: 'Сессия приостановлена', ended: 'Завершил сессию', broadcast: 'Все', queued: 'В очереди',
    running: 'Выполняется…', completed: '✓ Завершено', cancelled: 'Отменено', failed: 'Ошибка',
    online: 'снова онлайн', offline: 'потерял соединение', stale: 'данные устарели', authRevoked: 'авторизация отозвана',
    healthChanged: 'изменилось состояние', claimed: 'Взял задачу', released: 'Освободил задачу', blocked: 'Заблокирована',
    done: 'Завершил', comment: 'Комментарий к', generic: 'Событие активности',
  },
  ka: {
    unknownAgent: 'უცნობი აგენტი', ready: 'მზადაა სამუშაოდ', started: 'სესია დაიწყო', stopping: 'სესიას ასრულებს',
    suspended: 'სესია შეჩერებულია', ended: 'სესია დასრულდა', broadcast: 'ყველა', queued: 'რიგშია',
    running: 'მიმდინარეობს…', completed: '✓ დასრულდა', cancelled: 'გაუქმდა', failed: 'შეცდომა',
    online: 'კვლავ ონლაინ არის', offline: 'კავშირი დაკარგა', stale: 'მონაცემები მოძველდა', authRevoked: 'ავტორიზაცია გაუქმდა',
    healthChanged: 'მდგომარეობა შეიცვალა', claimed: 'აიღო დავალება', released: 'გაათავისუფლა დავალება', blocked: 'დაბლოკილია',
    done: 'დაასრულა', comment: 'კომენტარი', generic: 'აქტივობის განახლება',
  },
  es: {
    unknownAgent: 'Agente desconocido', ready: 'Listo para trabajar', started: 'Inició sesión', stopping: 'Finalizando sesión',
    suspended: 'Sesión pausada', ended: 'Finalizó sesión', broadcast: 'Todos', queued: 'En cola',
    running: 'Ejecutándose…', completed: '✓ Completado', cancelled: 'Cancelado', failed: 'Error',
    online: 'está en línea de nuevo', offline: 'perdió la conexión', stale: 'datos desactualizados', authRevoked: 'autorización revocada',
    healthChanged: 'estado cambiado', claimed: 'Tomó la tarea', released: 'Liberó la tarea', blocked: 'Bloqueada',
    done: 'Completó', comment: 'Comentario en', generic: 'Actualización de actividad',
  },
}

export type ActivityChatItem = {
  key: string
  kind: 'message' | 'service'
  createdAt: string
  actorId?: string
  actorName?: string
  content: string
  secondary?: string
  tone?: 'success' | 'stale' | 'critical' | 'neutral'
  taskNamespace?: string
  taskId?: string
  events: ActivityEventReadModel[]
}

export type ActivityRenderItem =
  | { kind: 'date'; key: string; label: string }
  | (ActivityChatItem & { showIdentity: boolean })

function textField(payload: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key]
    if (typeof value === 'string' && value.trim()) return value
    if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  }
  return undefined
}

function stateOf(event: ActivityEventReadModel): string {
  return (textField(event.payload, 'state', 'status', 'action', 'operational_status') ?? '').toLowerCase()
}

function actorFor(event: ActivityEventReadModel, copy: Copy): { id?: string; name: string } {
  if (event.message) return { id: event.message.senderAgentId, name: event.message.senderName || copy.unknownAgent }
  return {
    id: event.actorId,
    name: event.actorName ?? textField(event.payload, 'display_name', 'displayName', 'public_name', 'name') ?? copy.unknownAgent,
  }
}

function serviceEvent(event: ActivityEventReadModel): boolean {
  if (event.actorId || event.actorName || event.message) return false
  const value = (event.entityType + ' ' + event.eventType).toLowerCase()
  return /health|connection|auth|snapshot|runtime/.test(value)
}

function commandStatus(events: ActivityEventReadModel[], copy: Copy): string {
  const latest = events.at(-1)!
  const status = stateOf(latest)
  const duration = textField(latest.payload, 'duration', 'duration_ms', 'durationMs')
  if (/complete|success|done|finished/.test(status)) return copy.completed + (duration ? ' · ' + duration : '')
  if (/fail|error/.test(status)) return copy.failed + (textField(latest.payload, 'error', 'detail', 'message') ? ' · ' + textField(latest.payload, 'error', 'detail', 'message') : '')
  if (/cancel/.test(status)) return copy.cancelled
  if (/running|active|execut/.test(status)) return copy.running
  return copy.queued
}

function projectCommand(events: ActivityEventReadModel[], locale: Locale): ActivityChatItem {
  const copy = copyByLocale[locale]
  const first = events[0]
  const actor = actorFor(first, copy)
  const command = events.map((event) => textField(event.payload, 'command', 'cmd')).find(Boolean) ?? first.entityId
  return {
    key: 'command:' + first.entityId,
    kind: 'message',
    createdAt: first.createdAt,
    actorId: actor.id,
    actorName: actor.name,
    content: '$ ' + command,
    secondary: commandStatus(events, copy),
    events,
  }
}

function projectOne(event: ActivityEventReadModel, locale: Locale, serverName: string): ActivityChatItem {
  const copy = copyByLocale[locale]
  if (event.message) {
    const actor = actorFor(event, copy)
    const target = event.message.target
    const broadcast = /^(all|everyone|broadcast|fleet|\*)$/i.test(target)
    const recipientNames = event.message.recipients.map((recipient) => recipient.name).filter(Boolean)
    const routeLabel = broadcast ? copy.broadcast : recipientNames.length ? recipientNames.join(', ') : target
    return {
      key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name,
      content: event.message.text, secondary: '→ ' + routeLabel,
      taskNamespace: event.message.taskNamespace, taskId: event.message.taskId, events: [event],
    }
  }

  if (serviceEvent(event)) {
    const state = stateOf(event)
    const raw = (event.eventType + ' ' + state).toLowerCase()
    let content = serverName + ': ' + copy.healthChanged
    let tone: ActivityChatItem['tone'] = 'neutral'
    if (/revoked/.test(raw)) { content = serverName + ': ' + copy.authRevoked; tone = 'critical' }
    else if (/offline|disconnect|failed|error/.test(raw)) { content = serverName + ' ' + copy.offline; tone = 'critical' }
    else if (/stale/.test(raw)) { content = serverName + ': ' + copy.stale; tone = 'stale' }
    else if (/online|live|healthy/.test(raw) || event.payload.ok === true) { content = serverName + ' ' + copy.online; tone = 'success' }
    return { key: 'event:' + event.seq, kind: 'service', createdAt: event.createdAt, content, tone, events: [event] }
  }

  const actor = actorFor(event, copy)
  const state = stateOf(event)
  if (/logical_agent|work_session|session|agent/.test(event.eventType + ' ' + event.entityType)) {
    const content = state === 'armed' ? copy.ready
      : state === 'active' ? copy.started
        : state === 'stopping' ? copy.stopping
          : state === 'suspended' ? copy.suspended
            : state === 'ended' ? copy.ended
              : textField(event.payload, 'summary', 'message', 'detail', 'intent') ?? copy.generic
    return { key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name, content, events: [event] }
  }

  if (/task/.test(event.entityType + ' ' + event.eventType) || textField(event.payload, 'task_id', 'taskId')) {
    const taskId = textField(event.payload, 'task_id', 'taskId') ?? event.entityId
    const title = textField(event.payload, 'title')
    const action = (event.eventType + ' ' + state + ' ' + (textField(event.payload, 'action') ?? '')).toLowerCase()
    let content: string
    if (/claim/.test(action)) content = copy.claimed + ' ' + taskId
    else if (/release|unclaim/.test(action)) content = copy.released + ' ' + taskId
    else if (/block/.test(action)) content = taskId + ' → ' + copy.blocked
    else if (/done|complete/.test(action)) content = copy.done + ' ' + taskId
    else if (/comment/.test(action)) content = copy.comment + ' ' + taskId
    else content = taskId + (state ? ' · ' + state : '')
    return {
      key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name,
      content, secondary: title ?? textField(event.payload, 'comment', 'reason', 'blocker_reason'),
      taskNamespace: textField(event.payload, 'namespace', 'task_namespace'), taskId, events: [event],
    }
  }

  return {
    key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name,
    content: textField(event.payload, 'message', 'summary', 'detail', 'intent', 'state', 'status', 'action') ?? copy.generic,
    events: [event],
  }
}

export function projectActivity(events: ActivityEventReadModel[], locale: Locale, serverName: string): ActivityChatItem[] {
  const result: ActivityChatItem[] = []
  const commandIndex = new Map<string, number>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (event.entityType === 'command' || event.eventType.includes('command')) {
      const correlation = textField(event.payload, 'command_id', 'commandId') ?? event.entityId
      const existing = commandIndex.get(correlation)
      if (existing === undefined) {
        commandIndex.set(correlation, result.length)
        result.push(projectCommand([event], locale))
      } else {
        const prior = result[existing]
        result[existing] = projectCommand([...prior.events, event], locale)
      }
      continue
    }
    result.push(projectOne(event, locale, serverName))
  }
  return result
}

function localDay(value: string): string {
  const date = new Date(value)
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-')
}

export function renderActivity(items: ActivityChatItem[], locale: Locale): ActivityRenderItem[] {
  const result: ActivityRenderItem[] = []
  let day = ''
  let priorMessage: ActivityChatItem | undefined
  for (const item of items) {
    const itemDay = localDay(item.createdAt)
    if (itemDay !== day) {
      day = itemDay
      const date = new Date(item.createdAt)
      result.push({ kind: 'date', key: 'date:' + itemDay, label: new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'long', year: 'numeric' }).format(date) })
      priorMessage = undefined
    }
    if (item.kind === 'service') {
      result.push({ ...item, showIdentity: false })
      priorMessage = undefined
      continue
    }
    const delta = priorMessage ? new Date(item.createdAt).getTime() - new Date(priorMessage.createdAt).getTime() : Number.POSITIVE_INFINITY
    const sameActor = Boolean(priorMessage && (priorMessage.actorId ?? priorMessage.actorName) === (item.actorId ?? item.actorName))
    const showIdentity = !(sameActor && delta >= 0 && delta <= 120_000)
    result.push({ ...item, showIdentity })
    priorMessage = item
  }
  return result
}

export function actorHue(name: string): number {
  let hash = 0
  for (const char of name) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0
  return Math.abs(hash) % 360
}

export function activityTime(value: string): string {
  const date = new Date(value)
  const pad = (part: number) => String(part).padStart(2, '0')
  return pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds())
}

export function activityFullTimestamp(value: string): string {
  const date = new Date(value)
  const pad = (part: number) => String(part).padStart(2, '0')
  return pad(date.getDate()) + '.' + pad(date.getMonth() + 1) + '.' + date.getFullYear() + ' ' + activityTime(value)
}
