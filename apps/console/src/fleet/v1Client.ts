import { ConsoleHttpError, type FetchLike } from '../api/client'
import type {
  FleetProjectionEntity,
  FleetProjectionEvent,
  FleetProjectionEventPage,
  FleetProjectionSnapshot,
  FleetProjectionSource,
  FleetRuntimeOverlay,
} from './v1Types'

export type FleetV1Probe = {
  projectionEpoch: number
  projectionSeq: number
  role: 'owner' | 'follower'
  ownerNodeId: string
  sources: FleetProjectionSource[]
}

type RecordValue = Record<string, unknown>

function record(value: unknown, label: string): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_' + label)
  return value as RecordValue
}
function stringValue(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error('invalid_' + label)
  return value
}
function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}
function integer(value: unknown, label: string): number {
  if (!Number.isInteger(value) || Number(value) < 0) throw new Error('invalid_' + label)
  return Number(value)
}
function source(value: unknown): FleetProjectionSource {
  const item = record(value, 'source')
  const freshness = stringValue(item.freshness, 'freshness')
  if (!['fresh', 'stale', 'reset_required', 'unavailable'].includes(freshness)) {
    throw new Error('invalid_freshness')
  }
  return {
    sourceNodeId: stringValue(item.source_node_id, 'source_node_id'),
    sourceStreamGeneration: stringValue(item.source_stream_generation, 'source_stream_generation'),
    sourceSeq: integer(item.source_seq, 'source_seq'),
    freshness: freshness as FleetProjectionSource['freshness'],
    updatedAt: stringValue(item.updated_at, 'updated_at'),
  }
}
function entity(value: unknown): FleetProjectionEntity {
  const item = record(value, 'entity')
  return {
    sourceNodeId: stringValue(item.source_node_id, 'source_node_id'),
    entityType: stringValue(item.entity_type, 'entity_type'),
    entityId: stringValue(item.entity_id, 'entity_id'),
    entityRevision: integer(item.entity_revision, 'entity_revision'),
    payloadVersion: integer(item.payload_version, 'payload_version'),
    payload: record(item.payload ?? {}, 'payload'),
    authorityNodeId: optionalString(item.authority_node_id),
    authorityEpoch: item.authority_epoch == null ? undefined : integer(item.authority_epoch, 'authority_epoch'),
    sourceStreamGeneration: stringValue(item.source_stream_generation, 'source_stream_generation'),
    sourceSeq: integer(item.source_seq, 'source_seq'),
    projectionSeq: integer(item.projection_seq, 'projection_seq'),
    updatedAt: stringValue(item.updated_at, 'updated_at'),
  }
}
function overlay(value: unknown): FleetRuntimeOverlay {
  const item = record(value, 'overlay')
  const freshness = stringValue(item.freshness, 'freshness')
  if (!['fresh', 'stale', 'unavailable'].includes(freshness)) throw new Error('invalid_overlay_freshness')
  return {
    sourceNodeId: stringValue(item.source_node_id, 'source_node_id'),
    payload: record(item.payload ?? {}, 'payload'),
    observedAt: stringValue(item.observed_at, 'observed_at'),
    freshness: freshness as FleetRuntimeOverlay['freshness'],
  }
}
function event(value: unknown): FleetProjectionEvent {
  const item = record(value, 'event')
  return {
    projectionEpoch: integer(item.projection_epoch, 'projection_epoch'),
    projectionSeq: integer(item.projection_seq, 'projection_seq'),
    eventId: stringValue(item.event_id, 'event_id'),
    sourceNodeId: stringValue(item.source_node_id, 'source_node_id'),
    sourceStreamGeneration: stringValue(item.source_stream_generation, 'source_stream_generation'),
    sourceSeq: integer(item.source_seq, 'source_seq'),
    eventType: stringValue(item.event_type, 'event_type'),
    entityType: stringValue(item.entity_type, 'entity_type'),
    entityId: stringValue(item.entity_id, 'entity_id'),
    entityRevision: integer(item.entity_revision, 'entity_revision'),
    payloadVersion: integer(item.payload_version, 'payload_version'),
    payload: record(item.payload ?? {}, 'payload'),
    authorityNodeId: optionalString(item.authority_node_id),
    authorityEpoch: item.authority_epoch == null ? undefined : integer(item.authority_epoch, 'authority_epoch'),
    createdAt: stringValue(item.created_at, 'created_at'),
  }
}

export function decodeFleetV1Probe(value: unknown): FleetV1Probe {
  const item = record(value, 'probe')
  if (item.ok !== true) throw new Error('invalid_probe_ok')
  const role = stringValue(item.role, 'role')
  if (role !== 'owner' && role !== 'follower') throw new Error('invalid_role')
  return {
    projectionEpoch: integer(item.projection_epoch, 'projection_epoch'),
    projectionSeq: integer(item.projection_seq, 'projection_seq'),
    role,
    ownerNodeId: stringValue(item.owner_node_id, 'owner_node_id'),
    sources: Array.isArray(item.sources) ? item.sources.map(source) : [],
  }
}

export function decodeFleetV1Snapshot(value: unknown): FleetProjectionSnapshot {
  const item = record(value, 'snapshot')
  const role = stringValue(item.role, 'role')
  if (role !== 'owner' && role !== 'follower') throw new Error('invalid_role')
  return {
    fleetId: stringValue(item.fleet_id, 'fleet_id'),
    nodeId: stringValue(item.node_id, 'node_id'),
    ownerNodeId: stringValue(item.owner_node_id, 'owner_node_id'),
    role,
    projectionEpoch: integer(item.projection_epoch, 'projection_epoch'),
    projectionSeq: integer(item.projection_seq, 'projection_seq'),
    updatedAt: stringValue(item.updated_at, 'updated_at'),
    sources: Array.isArray(item.sources) ? item.sources.map(source) : [],
    entities: Array.isArray(item.entities) ? item.entities.map(entity) : [],
    runtimeOverlays: Array.isArray(item.runtime_overlays) ? item.runtime_overlays.map(overlay) : [],
  }
}

export function decodeFleetV1Events(value: unknown): FleetProjectionEventPage {
  const item = record(value, 'events')
  return {
    projectionEpoch: integer(item.projection_epoch, 'projection_epoch'),
    projectionSeq: integer(item.projection_seq, 'projection_seq'),
    oldestProjectionSeq:
      item.oldest_projection_seq == null ? undefined : integer(item.oldest_projection_seq, 'oldest_projection_seq'),
    newestProjectionSeq:
      item.newest_projection_seq == null ? undefined : integer(item.newest_projection_seq, 'newest_projection_seq'),
    resetRequired: item.reset_required === true,
    events: Array.isArray(item.events) ? item.events.map(event) : [],
  }
}

async function body(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    return {}
  }
}

export class FleetV1Client {
  readonly origin: string
  constructor(
    origin: string,
    private readonly accessToken: () => string,
    private readonly fetcher: FetchLike = fetch,
  ) {
    this.origin = new URL(origin).origin
  }

  probe(): Promise<FleetV1Probe> {
    return this.get('/console/fleet/v1/probe').then(decodeFleetV1Probe)
  }

  snapshot(): Promise<FleetProjectionSnapshot> {
    return this.get('/console/fleet/v1/snapshot').then(decodeFleetV1Snapshot)
  }

  events(since: number, limit = 500): Promise<FleetProjectionEventPage> {
    return this.request('/console/fleet/v1/activity', {
      method: 'POST',
      body: JSON.stringify({ since, limit }),
    }).then(decodeFleetV1Events)
  }

  private get(path: string): Promise<unknown> {
    return this.request(path)
  }

  private async request(path: string, init: RequestInit = {}): Promise<unknown> {
    let response: Response
    try {
      response = await this.fetcher(new URL(path, this.origin), {
        ...init,
        headers: {
          Accept: 'application/json',
          Authorization: 'Bearer ' + this.accessToken(),
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...init.headers,
        },
      })
    } catch {
      throw new ConsoleHttpError(0, 'network_error')
    }
    const value = await body(response)
    if (!response.ok) {
      const payload = value && typeof value === 'object' ? value as RecordValue : {}
      throw new ConsoleHttpError(
        response.status,
        typeof payload.error === 'string' ? payload.error : 'fleet_request_failed',
      )
    }
    return value
  }
}
