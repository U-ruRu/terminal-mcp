import type { KeyValueStorage } from '../auth/vault'

const STORAGE_KEY = 'terminal-mcp.console.diagnostics.v1'
const BUILD_KEY = 'terminal-mcp.console.last-build.v1'
export const DIAGNOSTIC_EVENT_LIMIT = 100

export type DiagnosticEvent = {
  seq: number
  at: string
  type: string
  instanceId?: string
  server?: string
  status?: string
  authStatus?: string
  code?: string
  detail?: string
}

type DiagnosticDocument = { version: 1; nextSeq: number; events: DiagnosticEvent[] }
type Listener = () => void

function clipped(value: string | undefined, max = 500): string | undefined {
  if (!value) return undefined
  const clean = value.replace(/\s+/g, ' ').trim()
  return clean ? clean.slice(0, max) : undefined
}

export function diagnosticErrorCode(value: string | undefined): string | undefined {
  if (!value) return undefined
  const diagnostic = /Diagnostic:\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)/i.exec(value)?.[1]
  if (diagnostic) return diagnostic
  const simple = value.trim()
  return /^[A-Za-z0-9_.-]{1,80}$/.test(simple) ? simple : undefined
}

function emptyDocument(): DiagnosticDocument {
  return { version: 1, nextSeq: 1, events: [] }
}

function parseDocument(raw: string | null): DiagnosticDocument {
  if (!raw) return emptyDocument()
  try {
    const value = JSON.parse(raw) as Partial<DiagnosticDocument>
    if (value.version !== 1 || !Array.isArray(value.events)) return emptyDocument()
    const events = value.events.filter((item): item is DiagnosticEvent =>
      Boolean(item && Number.isFinite(item.seq) && typeof item.at === 'string' && typeof item.type === 'string'),
    ).slice(-DIAGNOSTIC_EVENT_LIMIT)
    const maxSeq = events.reduce((max, item) => Math.max(max, item.seq), 0)
    return { version: 1, nextSeq: Math.max(maxSeq + 1, Number(value.nextSeq) || 1), events }
  } catch {
    return emptyDocument()
  }
}

export class BrowserDiagnosticJournal {
  private readonly listeners = new Set<Listener>()

  constructor(
    private readonly storage: KeyValueStorage = window.localStorage,
    private readonly now: () => Date = () => new Date(),
  ) {}

  append(event: Omit<DiagnosticEvent, 'seq' | 'at'> & { at?: string }): DiagnosticEvent {
    const document = parseDocument(this.storage.getItem(STORAGE_KEY))
    const entry: DiagnosticEvent = {
      seq: document.nextSeq,
      at: event.at ?? this.now().toISOString(),
      type: clipped(event.type, 80) ?? 'unknown',
      instanceId: clipped(event.instanceId, 160),
      server: clipped(event.server, 160),
      status: clipped(event.status, 80),
      authStatus: clipped(event.authStatus, 80),
      code: clipped(event.code, 80),
      detail: clipped(event.detail),
    }
    const events = [...document.events, entry].slice(-DIAGNOSTIC_EVENT_LIMIT)
    this.storage.setItem(STORAGE_KEY, JSON.stringify({ version: 1, nextSeq: entry.seq + 1, events }))
    for (const listener of this.listeners) listener()
    return entry
  }

  list(instanceId?: string): DiagnosticEvent[] {
    const events = parseDocument(this.storage.getItem(STORAGE_KEY)).events
    const filtered = instanceId ? events.filter((item) => !item.instanceId || item.instanceId === instanceId) : events
    return filtered.map((item) => ({ ...item }))
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  recordApplicationStart(version: string, build: string): void {
    const current = `${version} (${build})`
    const previous = this.storage.getItem(BUILD_KEY)
    if (previous && previous !== current) {
      this.append({ type: 'app_updated', detail: `${previous} -> ${current}` })
    }
    this.storage.setItem(BUILD_KEY, current)
    this.append({ type: 'app_started', detail: current })
  }

  exportText(instanceId?: string): string {
    return this.list(instanceId).map((event) => {
      const fields = [
        event.instanceId ? `instance=${event.instanceId}` : '',
        event.server ? `server=${event.server}` : '',
        event.status ? `status=${event.status}` : '',
        event.authStatus ? `auth=${event.authStatus}` : '',
        event.code ? `code=${event.code}` : '',
        event.detail ? `detail=${event.detail}` : '',
      ].filter(Boolean)
      return `${event.at} #${event.seq} ${event.type}${fields.length ? ' ' + fields.join(' ') : ''}`
    }).join('\n')
  }
}
