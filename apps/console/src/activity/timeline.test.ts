import { expect,test } from 'vitest'
import type { ActivityEventReadModel } from '../api/models'
import { mergeActivityEvents } from './timeline'
function event(seq: number, eventType: string, entityType: string): ActivityEventReadModel { return { seq, eventType, entityType, entityId: String(seq), payload: {}, createdAt: `2026-09-28T11:00:0${seq}Z` } }
test('replay deduplicates by sequence and preserves chronological sequence order', () => { expect(mergeActivityEvents([event(2,'task.updated','task'),event(4,'health.changed','health')],[event(1,'agent.started','agent'),event(2,'task.updated','task'),event(3,'message.created','message')]).map((item)=>item.seq)).toEqual([1,2,3,4]) })
