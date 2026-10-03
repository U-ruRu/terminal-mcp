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
    running: 'Running…', completed: 'Completed', cancelled: 'Cancelled', failed: 'Error',
    online: 'is online again', offline: 'lost connection', stale: 'data is stale', authRevoked: 'authorization revoked',
    healthChanged: 'health changed', claimed: 'Claimed task', released: 'Released task', blocked: 'Blocked',
    done: 'Completed', comment: 'Comment on', generic: 'Activity update',
  },
  ru: {
    unknownAgent: 'Неизвестный агент', ready: 'Готов к работе', started: 'Начал сессию', stopping: 'Завершает сессию',
    suspended: 'Сессия приостановлена', ended: 'Завершил сессию', broadcast: 'Все', queued: 'В очереди',
    running: 'Выполняется…', completed: 'Завершено', cancelled: 'Отменено', failed: 'Ошибка',
    online: 'снова онлайн', offline: 'потерял соединение', stale: 'данные устарели', authRevoked: 'авторизация отозвана',
    healthChanged: 'изменилось состояние', claimed: 'Взял задачу', released: 'Освободил задачу', blocked: 'Заблокирована',
    done: 'Завершил', comment: 'Комментарий к', generic: 'Событие активности',
  },
  ka: {
    unknownAgent: 'უცნობი აგენტი', ready: 'მზადაა სამუშაოდ', started: 'სესია დაიწყო', stopping: 'სესიას ასრულებს',
    suspended: 'სესია შეჩერებულია', ended: 'სესია დასრულდა', broadcast: 'ყველა', queued: 'რიგშია',
    running: 'მიმდინარეობს…', completed: 'დასრულდა', cancelled: 'გაუქმდა', failed: 'შეცდომა',
    online: 'კვლავ ონლაინ არის', offline: 'კავშირი დაკარგა', stale: 'მონაცემები მოძველდა', authRevoked: 'ავტორიზაცია გაუქმდა',
    healthChanged: 'მდგომარეობა შეიცვალა', claimed: 'აიღო დავალება', released: 'გაათავისუფლა დავალება', blocked: 'დაბლოკილია',
    done: 'დაასრულა', comment: 'კომენტარი', generic: 'აქტივობის განახლება',
  },
  es: {
    unknownAgent: 'Agente desconocido', ready: 'Listo para trabajar', started: 'Inició sesión', stopping: 'Finalizando sesión',
    suspended: 'Sesión pausada', ended: 'Finalizó sesión', broadcast: 'Todos', queued: 'En cola',
    running: 'Ejecutándose…', completed: 'Completado', cancelled: 'Cancelado', failed: 'Error',
    online: 'está en línea de nuevo', offline: 'perdió la conexión', stale: 'datos desactualizados', authRevoked: 'autorización revocada',
    healthChanged: 'estado cambiado', claimed: 'Tomó la tarea', released: 'Liberó la tarea', blocked: 'Bloqueada',
    done: 'Completó', comment: 'Comentario en', generic: 'Actualización de actividad',
  },
}

export type ActivityCommandEntry = {
  key: string
  createdAt: string
  label: string
  status: string
  statusKey: string
  duration?: string
  events: ActivityEventReadModel[]
}

export type ActivityChatItem = {
  key: string
  kind: 'message' | 'service'
  createdAt: string
  actorId?: string
  actorName?: string
  actorAnonymous?: boolean
  content: string
  secondary?: string
  tone?: 'success' | 'stale' | 'critical' | 'neutral'
  taskNamespace?: string
  taskId?: string
  commands?: ActivityCommandEntry[]
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

function actorIdentity(event: ActivityEventReadModel): string | undefined {
  if (event.message?.senderAgentId) return event.message.senderAgentId
  if (event.actorId) return event.actorId
  const logical = textField(event.payload, 'logical_agent_id', 'logicalAgentId', 'agent_id', 'agentId')
  if (logical) return logical
  const ownerKind = textField(event.payload, 'owner_kind', 'ownerKind')
  if (ownerKind === 'logical_agent' || ownerKind === 'agent') {
    const owner = textField(event.payload, 'owner_id', 'ownerId')
    if (owner) return owner
  }
  if (/^(logical_)?agent$/i.test(event.entityType) && event.entityId) return event.entityId
  return undefined
}

function isInternalIdentity(value: string | undefined): boolean {
  if (!value) return false
  const trimmed = value.trim()
  return /^(la[_-]|ws[_-]|device[_-]|logical_agent|work_session)/i.test(trimmed)
}

function publicName(value: string | undefined, identity?: string): string | undefined {
  const trimmed = value?.trim()
  if (!trimmed || trimmed === identity || isInternalIdentity(trimmed)) return undefined
  return trimmed
}

function actorFingerprint(identity: string): string {
  let hash = 2166136261
  for (const char of identity) {
    hash ^= char.charCodeAt(0)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36).toUpperCase().padStart(4, '0').slice(-4)
}

function anonymousActorName(identity: string, locale: Locale): string {
  const prefix = locale === 'ru' ? 'Агент' : locale === 'ka' ? 'აგენტი' : locale === 'es' ? 'Agente' : 'Agent'
  return prefix + ' · ' + actorFingerprint(identity)
}

function actorFor(
  event: ActivityEventReadModel,
  copy: Copy,
  locale: Locale,
  agentNames: Record<string, string> = {},
): { id?: string; name?: string; anonymous: boolean } {
  if (event.message) {
    const id = event.message.senderAgentId
    const resolvedName = publicName(id ? agentNames[id] : undefined, id) ?? publicName(event.message.senderName, id)
    return resolvedName ? { id, name: resolvedName, anonymous: false } : id
      ? { id, name: anonymousActorName(id, locale), anonymous: true }
      : { anonymous: false }
  }
  const id = actorIdentity(event)
  const resolvedName = publicName(id ? agentNames[id] : undefined, id)
    ?? publicName(event.actorName, id)
    ?? publicName(textField(event.payload, 'display_name', 'displayName', 'public_name', 'publicName'), id)
  if (resolvedName) return { id, name: resolvedName, anonymous: false }
  if (id) return { id, name: anonymousActorName(id, locale), anonymous: true }
  return { anonymous: false }
}

function recipientLabel(
  idOrName: string,
  explicitName: string | undefined,
  locale: Locale,
  agentNames: Record<string, string>,
): string {
  if (/^(all|everyone|broadcast|fleet|\*)$/i.test(idOrName)) return copyByLocale[locale].broadcast
  const stableId = isInternalIdentity(idOrName) ? idOrName : undefined
  return publicName(stableId ? agentNames[stableId] : explicitName, stableId)
    ?? publicName(explicitName, stableId)
    ?? (stableId ? anonymousActorName(stableId, locale) : idOrName)
}

function serviceEvent(event: ActivityEventReadModel): boolean {
  if (event.message || event.actorName || actorIdentity(event)) return false
  return true
}

function commandStatusKey(events: ActivityEventReadModel[]): string {
  for (const event of [...events].reverse()) {
    const status = stateOf(event)
    if (status) return status
  }
  return 'queued'
}

function commandDuration(events: ActivityEventReadModel[]): string | undefined {
  for (const event of [...events].reverse()) {
    const explicit = textField(event.payload, 'duration', 'duration_ms', 'durationMs')
    if (explicit) {
      if (/^\d+(?:\.\d+)?$/.test(explicit) && ['duration_ms', 'durationMs'].some((key) => key in event.payload)) {
        return (Number(explicit) / 1000).toFixed(Number(explicit) % 1000 === 0 ? 0 : 1) + ' s'
      }
      return explicit
    }
  }
  const terminal = [...events].reverse().find((event) => /complete|success|done|finished|fail|error|cancel/.test(stateOf(event)))
  if (!terminal) return undefined
  const milliseconds = new Date(terminal.createdAt).getTime() - new Date(events[0].createdAt).getTime()
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return undefined
  const seconds = milliseconds / 1000
  return (seconds < 10 ? seconds.toFixed(1) : seconds.toFixed(0)).replace(/\.0$/, '') + ' s'
}

function commandStatus(events: ActivityEventReadModel[], copy: Copy): string {
  const status = commandStatusKey(events)
  const duration = commandDuration(events)
  if (/complete|success|done|finished/.test(status)) return copy.completed + (duration ? ' · ' + duration : '')
  if (/fail|error/.test(status)) {
    const latest = [...events].reverse().find((event) => textField(event.payload, 'error', 'detail', 'message'))
    const shortError = latest ? textField(latest.payload, 'error', 'detail', 'message') : undefined
    return copy.failed + (shortError ? ' · ' + shortError : '')
  }
  if (/cancel/.test(status)) return copy.cancelled
  if (/running|active|execut/.test(status)) return copy.running
  return copy.queued
}

function commandSummary(count: number, locale: Locale): string {
  if (count <= 1) {
    if (locale === 'ru') return 'Вызвал команду'
    if (locale === 'ka') return 'გამოიძახა ბრძანება'
    if (locale === 'es') return 'Ejecutó un comando'
    return 'Ran a command'
  }
  if (locale === 'ru') {
    const mod100 = count % 100
    const mod10 = count % 10
    const noun = mod100 >= 11 && mod100 <= 14 ? 'команд' : mod10 === 1 ? 'команду' : mod10 >= 2 && mod10 <= 4 ? 'команды' : 'команд'
    return 'Вызвал ' + count + ' ' + noun
  }
  if (locale === 'ka') return 'გამოიძახა ' + count + ' ბრძანება'
  if (locale === 'es') return 'Ejecutó ' + count + ' comandos'
  return 'Ran ' + count + ' commands'
}

function commandBatchStatus(commands: ActivityCommandEntry[], locale: Locale): string | undefined {
  if (commands.length <= 1) return commands[0]?.status
  const groups = [
    { match: /complete|success|done|finished/, ru: 'завершена', en: 'completed', es: 'completada', ka: 'დასრულდა' },
    { match: /cancel/, ru: 'отменена', en: 'cancelled', es: 'cancelada', ka: 'გაუქმდა' },
    { match: /fail|error/, ru: 'с ошибкой', en: 'failed', es: 'con error', ka: 'შეცდომით' },
    { match: /running|active|execut/, ru: 'выполняется', en: 'running', es: 'en ejecución', ka: 'მიმდინარეობს' },
    { match: /.*/, ru: 'в очереди', en: 'queued', es: 'en cola', ka: 'რიგშია' },
  ]
  const counts = new Map<(typeof groups)[number], number>()
  for (const command of commands) {
    const group = groups.find((candidate) => candidate.match.test(command.statusKey)) ?? groups.at(-1)!
    counts.set(group, (counts.get(group) ?? 0) + 1)
  }
  const parts: string[] = []
  for (const group of groups) {
    const count = counts.get(group) ?? 0
    if (!count) continue
    const word = locale === 'ru' ? group.ru : locale === 'es' ? group.es : locale === 'ka' ? group.ka : group.en
    parts.push(count + ' ' + word)
  }
  return parts.join(' · ')
}

function commandLabel(events: ActivityEventReadModel[], locale: Locale): string {
  const rawCommand = events.map((event) => textField(event.payload, 'command', 'cmd')).find(Boolean)
  if (rawCommand) return '$ ' + rawCommand
  const operation = events.map((event) => textField(event.payload, 'command_type', 'commandType', 'operation', 'tool')).find(Boolean)
  if (operation) return operation.replace(/_/g, ' ')
  return locale === 'ru' ? 'Команда' : locale === 'ka' ? 'ბრძანება' : locale === 'es' ? 'Comando' : 'Command'
}

function projectCommand(events: ActivityEventReadModel[], locale: Locale, serverName: string, agentNames: Record<string, string>): ActivityChatItem {
  const copy = copyByLocale[locale]
  const first = events[0]
  const actorEvent = events.find((event) =>
    event.actorName || event.actorId || textField(event.payload, 'logical_agent_id', 'logicalAgentId', 'agent_id', 'agentId', 'display_name', 'displayName', 'public_name', 'name')
  ) ?? first
  const actor = actorFor(actorEvent, copy, locale, agentNames)
  const correlation = textField(first.payload, 'command_id', 'commandId') ?? first.entityId
  const duration = commandDuration(events)
  const entry: ActivityCommandEntry = {
    key: 'command:' + correlation,
    createdAt: first.createdAt,
    label: commandLabel(events, locale),
    status: commandStatus(events, copy),
    statusKey: commandStatusKey(events),
    duration,
    events,
  }
  if (!actor.name) {
    return {
      key: 'command-batch:' + correlation,
      kind: 'service',
      createdAt: first.createdAt,
      content: serverName + ': ' + entry.label + ' · ' + entry.status,
      commands: [entry],
      events,
    }
  }
  return {
    key: 'command-batch:' + correlation,
    kind: 'message',
    createdAt: first.createdAt,
    actorId: actor.id,
    actorName: actor.name,
    actorAnonymous: actor.anonymous,
    content: entry.label,
    secondary: entry.status,
    commands: [entry],
    events,
  }
}

function projectOne(event: ActivityEventReadModel, locale: Locale, serverName: string, agentNames: Record<string, string>): ActivityChatItem {
  const copy = copyByLocale[locale]
  if (event.message) {
    const actor = actorFor(event, copy, locale, agentNames)
    const target = event.message.target
    const recipientNames = event.message.recipients.map((recipient) =>
      recipientLabel(recipient.agentId ?? recipient.name, recipient.name, locale, agentNames)
    ).filter(Boolean)
    const routeLabel = recipientNames.length
      ? recipientNames.join(', ')
      : recipientLabel(target, undefined, locale, agentNames)
    return {
      key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name, actorAnonymous: actor.anonymous,
      content: event.message.text, secondary: '→ ' + routeLabel,
      taskNamespace: event.message.taskNamespace, taskId: event.message.taskId, events: [event],
    }
  }

  if (serviceEvent(event)) {
    const state = stateOf(event)
    const raw = (event.eventType + ' ' + state).toLowerCase()
    const summary = textField(event.payload, 'message', 'summary', 'detail', 'intent')
    let content = summary ? serverName + ': ' + summary : serverName + ': ' + copy.generic
    let tone: ActivityChatItem['tone'] = 'neutral'
    if (/revoked/.test(raw)) { content = serverName + ': ' + copy.authRevoked; tone = 'critical' }
    else if (/offline|disconnect|failed|error/.test(raw)) { content = serverName + ' ' + copy.offline; tone = 'critical' }
    else if (/stale/.test(raw)) { content = serverName + ': ' + copy.stale; tone = 'stale' }
    else if (/online|live|healthy/.test(raw) || event.payload.ok === true) { content = serverName + ' ' + copy.online; tone = 'success' }
    else if (/health/.test(raw)) content = serverName + ': ' + copy.healthChanged
    return { key: 'event:' + event.seq, kind: 'service', createdAt: event.createdAt, content, tone, events: [event] }
  }

  const actor = actorFor(event, copy, locale, agentNames)
  const state = stateOf(event)
  if (/logical_agent|work_session|session|agent/.test(event.eventType + ' ' + event.entityType)) {
    const content = state === 'armed' ? copy.ready
      : state === 'active' ? copy.started
        : state === 'stopping' ? copy.stopping
          : state === 'suspended' ? copy.suspended
            : state === 'ended' ? copy.ended
              : textField(event.payload, 'summary', 'message', 'detail', 'intent') ?? copy.generic
    return { key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name, actorAnonymous: actor.anonymous, content, events: [event] }
  }

  if (/task/.test(event.entityType + ' ' + event.eventType) || textField(event.payload, 'task_id', 'taskId')) {
    const rawTaskId = textField(event.payload, 'task_id', 'taskId') ?? event.entityId
    const taskId = rawTaskId.includes('/') ? rawTaskId.slice(rawTaskId.lastIndexOf('/') + 1) : rawTaskId
    const title = textField(event.payload, 'title')
    const action = (event.eventType + ' ' + state + ' ' + (textField(event.payload, 'action') ?? '')).toLowerCase()
    let content: string
    if (/claim/.test(action)) content = copy.claimed + ' ' + taskId
    else if (/release|unclaim/.test(action)) content = copy.released + ' ' + taskId
    else if (/block/.test(action)) content = taskId + ' → ' + copy.blocked
    else if (/done|complete/.test(action)) content = copy.done + ' ' + taskId
    else if (/comment/.test(action)) content = copy.comment + ' ' + taskId
    else content = taskId + (state ? ' · ' + state : '')
    const detail = /comment/.test(action)
      ? textField(event.payload, 'comment', 'message', 'detail')
      : /block/.test(action)
        ? textField(event.payload, 'blocker_reason', 'reason', 'detail')
        : title
    return {
      key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name, actorAnonymous: actor.anonymous,
      content, secondary: detail,
      taskNamespace: textField(event.payload, 'namespace', 'task_namespace'), taskId, events: [event],
    }
  }

  return {
    key: 'event:' + event.seq, kind: 'message', createdAt: event.createdAt, actorId: actor.id, actorName: actor.name, actorAnonymous: actor.anonymous,
    content: textField(event.payload, 'message', 'summary', 'detail', 'intent', 'state', 'status', 'action') ?? copy.generic,
    events: [event],
  }
}

export function projectActivity(
  events: ActivityEventReadModel[],
  locale: Locale,
  serverName: string,
  agentNames: Record<string, string> = {},
): ActivityChatItem[] {
  type SourceGroup = { kind: 'command' | 'message' | 'raw'; events: ActivityEventReadModel[] }
  const groups: SourceGroup[] = []
  const commandIndex = new Map<string, number>()
  const messageIndex = new Map<string, number>()

  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (event.entityType === 'command' || event.eventType.includes('command')) {
      const correlation = textField(event.payload, 'command_id', 'commandId') ?? event.entityId
      const existing = commandIndex.get(correlation)
      if (existing === undefined) {
        commandIndex.set(correlation, groups.length)
        groups.push({ kind: 'command', events: [event] })
      } else groups[existing].events.push(event)
      continue
    }
    if (event.entityType === 'message' || event.message) {
      const correlation = event.message?.messageHash ?? event.entityId
      const existing = messageIndex.get(correlation)
      if (existing === undefined) {
        messageIndex.set(correlation, groups.length)
        groups.push({ kind: 'message', events: [event] })
      } else groups[existing].events.push(event)
      continue
    }
    groups.push({ kind: 'raw', events: [event] })
  }

  const projected: ActivityChatItem[] = []
  for (const group of groups) {
    if (group.kind === 'command') {
      projected.push(projectCommand(group.events, locale, serverName, agentNames))
      continue
    }
    if (group.kind === 'message') {
      const rich = group.events.find((event) => event.message)
      if (!rich?.message) continue
      const item = projectOne(rich, locale, serverName, agentNames)
      item.key = 'message:' + rich.message.messageHash
      item.events = group.events
      projected.push(item)
      continue
    }
    projected.push(projectOne(group.events[0], locale, serverName, agentNames))
  }

  const coalesced: ActivityChatItem[] = []
  const lifecycleText = new Set(Object.values(copyByLocale).flatMap((copy) => [copy.ready, copy.started, copy.stopping, copy.suspended, copy.ended]))
  for (const item of projected) {
    const prior = coalesced.at(-1)
    if (item.kind === 'message' && prior?.kind === 'message') {
      const priorActor = prior.actorId ?? prior.actorName
      const currentActor = item.actorId ?? item.actorName
      const delta = new Date(item.createdAt).getTime() - new Date(prior.createdAt).getTime()
      const sameActor = Boolean(priorActor && priorActor === currentActor)

      if (sameActor && delta >= 0 && delta <= 2_000 && lifecycleText.has(prior.content) && lifecycleText.has(item.content)) {
        const isDuplicate = prior.content === item.content
        const isReadyToActive = prior.content === copyByLocale[locale].ready && item.content === copyByLocale[locale].started
        const isStoppingToFinal = prior.content === copyByLocale[locale].stopping
          && (item.content === copyByLocale[locale].ended || item.content === copyByLocale[locale].suspended)
        if (isDuplicate || isReadyToActive || isStoppingToFinal) {
          coalesced[coalesced.length - 1] = {
            ...item,
            key: prior.key,
            createdAt: prior.createdAt,
            events: [...prior.events, ...item.events],
          }
          continue
        }
      }

      if (sameActor && item.taskId && prior.taskId === item.taskId && item.content === prior.content && delta >= 0 && delta <= 2_000) {
        coalesced[coalesced.length - 1] = {
          ...prior,
          secondary: item.secondary ?? prior.secondary,
          events: [...prior.events, ...item.events],
        }
        continue
      }

      if (item.commands?.length && prior.commands?.length) {
        const deltaCommand = new Date(item.createdAt).getTime() - new Date(prior.commands.at(-1)?.createdAt ?? prior.createdAt).getTime()
        if (sameActor && deltaCommand >= 0 && deltaCommand <= 120_000) {
          const commands = [...prior.commands, ...item.commands]
          coalesced[coalesced.length - 1] = {
            ...prior,
            content: commandSummary(commands.length, locale),
            secondary: commandBatchStatus(commands, locale),
            commands,
            events: [...prior.events, ...item.events],
          }
          continue
        }
      }
    }
    coalesced.push(item)
  }
  return coalesced
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
