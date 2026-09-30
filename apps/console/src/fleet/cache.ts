import type {
  FleetCacheView,
  FleetCachedActivity,
  FleetDurableCacheState,
  FleetGatewayQuality,
  FleetProjectionEntity,
  FleetProjectionEvent,
  FleetProjectionEventPage,
  FleetProjectionSnapshot,
  FleetRuntimeOverlay,
} from './v1Types'
import { entityCacheKey } from './v1Types'

const DB_VERSION = 1
const DEFAULT_ACTIVITY_LIMIT = 500

type StateRecord = {
  key: 'state'
  version: 1
  fleetId?: string
  projectionEpoch: number
  appliedProjectionSeq: number
  preferredIngressId?: string
  updatedAt: number
}
type EntityRecord = FleetProjectionEntity & { key: string }

export class FleetCacheError extends Error {
  constructor(readonly code: string) {
    super(code)
    this.name = 'FleetCacheError'
  }
}

export interface FleetProjectionCache {
  restore(): Promise<FleetCacheView>
  applySnapshot(snapshot: FleetProjectionSnapshot): Promise<FleetCacheView>
  applyEvents(page: FleetProjectionEventPage): Promise<FleetCacheView>
  replaceRuntimeOverlays(overlays: FleetRuntimeOverlay[]): Promise<FleetCacheView>
  setPreferredIngress(candidateId?: string): Promise<FleetCacheView>
  putGatewayQuality(quality: FleetGatewayQuality): Promise<FleetCacheView>
  clear(): Promise<void>
}

function emptyState(now = Date.now()): FleetDurableCacheState {
  return {
    version: 1,
    projectionEpoch: 0,
    appliedProjectionSeq: 0,
    entities: [],
    sources: [],
    activity: [],
    gatewayQuality: [],
    updatedAt: now,
  }
}

function removalEvent(event: FleetProjectionEvent): boolean {
  return event.payload.deleted === true || /(?:deleted|removed|released|resolved)$/.test(event.eventType)
}

function applyEntityEvent(
  entities: Map<string, FleetProjectionEntity>,
  event: FleetProjectionEvent,
): void {
  const key = entityCacheKey(event)
  const current = entities.get(key)
  if (current && current.entityRevision > event.entityRevision) return
  if (removalEvent(event)) {
    entities.delete(key)
    return
  }
  entities.set(key, {
    sourceNodeId: event.sourceNodeId,
    entityType: event.entityType,
    entityId: event.entityId,
    entityRevision: event.entityRevision,
    payloadVersion: event.payloadVersion,
    payload: { ...event.payload },
    authorityNodeId: event.authorityNodeId,
    authorityEpoch: event.authorityEpoch,
    sourceStreamGeneration: event.sourceStreamGeneration,
    sourceSeq: event.sourceSeq,
    projectionSeq: event.projectionSeq,
    updatedAt: event.createdAt,
  })
}

export function reduceSnapshot(
  prior: FleetDurableCacheState,
  snapshot: FleetProjectionSnapshot,
  now = Date.now(),
): FleetDurableCacheState {
  return {
    version: 1,
    fleetId: snapshot.fleetId,
    projectionEpoch: snapshot.projectionEpoch,
    appliedProjectionSeq: snapshot.projectionSeq,
    preferredIngressId: prior.preferredIngressId,
    entities: snapshot.entities.map((item) => ({ ...item, payload: { ...item.payload } })),
    sources: snapshot.sources.map((item) => ({ ...item })),
    activity: prior.projectionEpoch === snapshot.projectionEpoch ? [...prior.activity] : [],
    gatewayQuality: prior.gatewayQuality.map((item) => ({ ...item })),
    updatedAt: now,
  }
}

export function reduceEvents(
  prior: FleetDurableCacheState,
  page: FleetProjectionEventPage,
  activityLimit = DEFAULT_ACTIVITY_LIMIT,
  now = Date.now(),
): FleetDurableCacheState {
  if (page.resetRequired) throw new FleetCacheError('projection_reset_required')
  if (prior.projectionEpoch === 0) throw new FleetCacheError('projection_snapshot_required')
  if (page.projectionEpoch !== prior.projectionEpoch) throw new FleetCacheError('projection_epoch_changed')

  const entities = new Map(
    prior.entities.map((item) => [entityCacheKey(item), { ...item, payload: { ...item.payload } }]),
  )
  const activity = new Map(prior.activity.map((item) => [item.projectionSeq, { ...item }]))
  let cursor = prior.appliedProjectionSeq

  for (const event of [...page.events].sort((a, b) => a.projectionSeq - b.projectionSeq)) {
    if (event.projectionEpoch !== prior.projectionEpoch) throw new FleetCacheError('projection_epoch_changed')
    if (event.projectionSeq <= cursor) continue
    if (event.projectionSeq !== cursor + 1) throw new FleetCacheError('projection_cursor_gap')
    applyEntityEvent(entities, event)
    activity.set(event.projectionSeq, {
      projectionSeq: event.projectionSeq,
      eventId: event.eventId,
      sourceNodeId: event.sourceNodeId,
      eventType: event.eventType,
      entityType: event.entityType,
      entityId: event.entityId,
      createdAt: event.createdAt,
    })
    cursor = event.projectionSeq
  }

  return {
    ...prior,
    entities: [...entities.values()],
    activity: [...activity.values()]
      .sort((a, b) => a.projectionSeq - b.projectionSeq)
      .slice(-Math.max(1, activityLimit)),
    appliedProjectionSeq: cursor,
    updatedAt: now,
  }
}

function request<T>(value: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    value.onsuccess = () => resolve(value.result)
    value.onerror = () => reject(value.error ?? new FleetCacheError('indexeddb_request_failed'))
  })
}

function transactionDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve()
    tx.onabort = () => reject(tx.error ?? new FleetCacheError('indexeddb_transaction_aborted'))
    tx.onerror = () => reject(tx.error ?? new FleetCacheError('indexeddb_transaction_failed'))
  })
}

export class BrowserFleetProjectionCache implements FleetProjectionCache {
  private runtimeOverlays: FleetRuntimeOverlay[] = []

  constructor(
    private readonly databaseName = 'terminal-mcp.console.fleet.v1',
    private readonly indexedDb: IDBFactory = indexedDB,
    private readonly now: () => number = Date.now,
    private readonly activityLimit = DEFAULT_ACTIVITY_LIMIT,
  ) {}

  async restore(): Promise<FleetCacheView> {
    const db = await this.open()
    try {
      const tx = db.transaction(['state', 'entities', 'sources', 'activity', 'gateway'], 'readonly')
      const done = transactionDone(tx)
      const [stateRecord, entityRows, sourceRows, activityRows, gatewayRows] = await Promise.all([
        request(tx.objectStore('state').get('state')) as Promise<StateRecord | undefined>,
        request(tx.objectStore('entities').getAll()) as Promise<EntityRecord[]>,
        request(tx.objectStore('sources').getAll()) as Promise<FleetDurableCacheState['sources']>,
        request(tx.objectStore('activity').getAll()) as Promise<FleetCachedActivity[]>,
        request(tx.objectStore('gateway').getAll()) as Promise<FleetGatewayQuality[]>,
      ])
      await done
      const durable: FleetDurableCacheState = stateRecord
        ? {
            version: 1,
            fleetId: stateRecord.fleetId,
            projectionEpoch: stateRecord.projectionEpoch,
            appliedProjectionSeq: stateRecord.appliedProjectionSeq,
            preferredIngressId: stateRecord.preferredIngressId,
            entities: entityRows.map((row) => {
              const item = { ...row }
              delete (item as Partial<EntityRecord>).key
              return item
            }),
            sources: sourceRows,
            activity: activityRows.sort((a, b) => a.projectionSeq - b.projectionSeq),
            gatewayQuality: gatewayRows,
            updatedAt: stateRecord.updatedAt,
          }
        : emptyState(this.now())
      return { ...durable, runtimeOverlays: structuredClone(this.runtimeOverlays) }
    } finally {
      db.close()
    }
  }

  async applySnapshot(snapshot: FleetProjectionSnapshot): Promise<FleetCacheView> {
    const prior = await this.restore()
    const next = reduceSnapshot(prior, snapshot, this.now())
    const db = await this.open()
    try {
      const tx = db.transaction(['state', 'entities', 'sources', 'activity'], 'readwrite')
      const done = transactionDone(tx)
      tx.objectStore('entities').clear()
      tx.objectStore('sources').clear()
      if (prior.projectionEpoch !== snapshot.projectionEpoch) tx.objectStore('activity').clear()
      for (const item of next.entities) {
        tx.objectStore('entities').put({ ...item, key: entityCacheKey(item) } satisfies EntityRecord)
      }
      for (const item of next.sources) tx.objectStore('sources').put(item)
      tx.objectStore('state').put(this.stateRecord(next))
      await done
      this.runtimeOverlays = structuredClone(snapshot.runtimeOverlays)
    } finally {
      db.close()
    }
    return this.restore()
  }

  async applyEvents(page: FleetProjectionEventPage): Promise<FleetCacheView> {
    const prior = await this.restore()
    const next = reduceEvents(prior, page, this.activityLimit, this.now())
    const db = await this.open()
    try {
      const tx = db.transaction(['state', 'entities', 'activity'], 'readwrite')
      const done = transactionDone(tx)
      const entities = tx.objectStore('entities')
      entities.clear()
      for (const item of next.entities) entities.put({ ...item, key: entityCacheKey(item) } satisfies EntityRecord)
      const activity = tx.objectStore('activity')
      activity.clear()
      for (const item of next.activity) activity.put(item)
      tx.objectStore('state').put(this.stateRecord(next))
      await done
    } finally {
      db.close()
    }
    return this.restore()
  }

  async replaceRuntimeOverlays(overlays: FleetRuntimeOverlay[]): Promise<FleetCacheView> {
    this.runtimeOverlays = structuredClone(overlays)
    return this.restore()
  }

  async setPreferredIngress(candidateId?: string): Promise<FleetCacheView> {
    const prior = await this.restore()
    const next = { ...prior, preferredIngressId: candidateId, updatedAt: this.now() }
    const db = await this.open()
    try {
      const tx = db.transaction('state', 'readwrite')
      const done = transactionDone(tx)
      tx.objectStore('state').put(this.stateRecord(next))
      await done
    } finally {
      db.close()
    }
    return this.restore()
  }

  async putGatewayQuality(quality: FleetGatewayQuality): Promise<FleetCacheView> {
    const db = await this.open()
    try {
      const tx = db.transaction('gateway', 'readwrite')
      const done = transactionDone(tx)
      tx.objectStore('gateway').put({ ...quality })
      await done
    } finally {
      db.close()
    }
    return this.restore()
  }

  async clear(): Promise<void> {
    this.runtimeOverlays = []
    await new Promise<void>((resolve, reject) => {
      const value = this.indexedDb.deleteDatabase(this.databaseName)
      value.onsuccess = () => resolve()
      value.onerror = () => reject(value.error ?? new FleetCacheError('indexeddb_delete_failed'))
      value.onblocked = () => reject(new FleetCacheError('indexeddb_delete_blocked'))
    })
  }

  private stateRecord(
    value: Pick<
      FleetDurableCacheState,
      'fleetId' | 'projectionEpoch' | 'appliedProjectionSeq' | 'preferredIngressId' | 'updatedAt'
    >,
  ): StateRecord {
    return {
      key: 'state',
      version: 1,
      fleetId: value.fleetId,
      projectionEpoch: value.projectionEpoch,
      appliedProjectionSeq: value.appliedProjectionSeq,
      preferredIngressId: value.preferredIngressId,
      updatedAt: value.updatedAt,
    }
  }

  private async open(): Promise<IDBDatabase> {
    const opening = this.indexedDb.open(this.databaseName, DB_VERSION)
    opening.onupgradeneeded = () => {
      const db = opening.result
      if (!db.objectStoreNames.contains('state')) db.createObjectStore('state', { keyPath: 'key' })
      if (!db.objectStoreNames.contains('entities')) db.createObjectStore('entities', { keyPath: 'key' })
      if (!db.objectStoreNames.contains('sources')) db.createObjectStore('sources', { keyPath: 'sourceNodeId' })
      if (!db.objectStoreNames.contains('activity')) db.createObjectStore('activity', { keyPath: 'projectionSeq' })
      if (!db.objectStoreNames.contains('gateway')) db.createObjectStore('gateway', { keyPath: 'candidateId' })
    }
    return request(opening)
  }
}

export class MemoryFleetProjectionCache implements FleetProjectionCache {
  private durable = emptyState()
  private runtimeOverlays: FleetRuntimeOverlay[] = []
  constructor(
    private readonly activityLimit = DEFAULT_ACTIVITY_LIMIT,
    private readonly now: () => number = Date.now,
  ) {}

  async restore(): Promise<FleetCacheView> {
    return { ...structuredClone(this.durable), runtimeOverlays: structuredClone(this.runtimeOverlays) }
  }
  async applySnapshot(snapshot: FleetProjectionSnapshot): Promise<FleetCacheView> {
    this.durable = reduceSnapshot(this.durable, snapshot, this.now())
    this.runtimeOverlays = structuredClone(snapshot.runtimeOverlays)
    return this.restore()
  }
  async applyEvents(page: FleetProjectionEventPage): Promise<FleetCacheView> {
    this.durable = reduceEvents(this.durable, page, this.activityLimit, this.now())
    return this.restore()
  }
  async replaceRuntimeOverlays(overlays: FleetRuntimeOverlay[]): Promise<FleetCacheView> {
    this.runtimeOverlays = structuredClone(overlays)
    return this.restore()
  }
  async setPreferredIngress(candidateId?: string): Promise<FleetCacheView> {
    this.durable = { ...this.durable, preferredIngressId: candidateId, updatedAt: this.now() }
    return this.restore()
  }
  async putGatewayQuality(quality: FleetGatewayQuality): Promise<FleetCacheView> {
    const byId = new Map(this.durable.gatewayQuality.map((item) => [item.candidateId, item]))
    byId.set(quality.candidateId, structuredClone(quality))
    this.durable = { ...this.durable, gatewayQuality: [...byId.values()], updatedAt: this.now() }
    return this.restore()
  }
  async clear(): Promise<void> {
    this.durable = emptyState(this.now())
    this.runtimeOverlays = []
  }
}
