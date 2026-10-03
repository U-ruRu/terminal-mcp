import type { ActivityFeedReadModel } from '../api/models'
import type { ActivityCategory } from './timeline'
import type { ActivityScrollAnchor } from './scrollAnchor'

export type ActivityFeedState = {
  events: ActivityFeedReadModel['events']
  cursor: number
  highWater: number
  oldestSeq?: number
  gap: boolean
  initialized: boolean
  loading: boolean
  historyMode: boolean
  error?: string
}

export type PersistedActivityState = {
  feed: ActivityFeedState
  category: ActivityCategory
  agentId: string
  anchor?: ActivityScrollAnchor
  newItemsCount: number
  stickToBottom: boolean
}

export const activityStateCache = new Map<string, PersistedActivityState>()

export function resetActivityStateCacheForTests(): void {
  activityStateCache.clear()
}
