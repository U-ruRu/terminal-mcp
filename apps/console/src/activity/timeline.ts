import type { ActivityEventReadModel } from '../api/models'

export type ActivityCategory = 'all' | 'messages' | 'tasks' | 'intents' | 'sessions' | 'health' | 'commands'

export function activityCategory(event: ActivityEventReadModel): ActivityCategory {
  if (event.entityType === 'message') return 'messages'
  if (event.entityType === 'task') return 'tasks'
  if (event.entityType === 'health') return 'health'
  if (event.entityType === 'command') return 'commands'
  if (event.eventType.includes('intent')) return 'intents'
  if (event.entityType === 'agent' || event.eventType.startsWith('agent.')) return 'sessions'
  return 'all'
}

export function mergeActivityEvents(
  current: ActivityEventReadModel[],
  incoming: ActivityEventReadModel[],
): ActivityEventReadModel[] {
  const bySeq = new Map<number, ActivityEventReadModel>()
  for (const event of current) bySeq.set(event.seq, event)
  for (const event of incoming) bySeq.set(event.seq, event)
  return [...bySeq.values()].sort((left, right) => left.seq - right.seq)
}

export function filterActivityEvents(
  events: ActivityEventReadModel[],
  category: ActivityCategory,
): ActivityEventReadModel[] {
  if (category === 'all') return events
  return events.filter((event) => activityCategory(event) === category)
}
