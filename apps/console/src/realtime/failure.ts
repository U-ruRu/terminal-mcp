import { ConsoleHttpError } from '../api/client'
import { ConsoleContractError } from '../api/contracts'

export type RealtimeFailureStage =
  | 'initial_snapshot'
  | 'snapshot_resync'
  | 'snapshot_refresh'
  | 'websocket_handshake'
  | 'websocket_frame'
  | 'websocket_closed'

export type RealtimeFailure = {
  stage: RealtimeFailureStage
  code: string
  detail: string
}

// Diagnostics must never persist server bodies, credentials, signed WebSocket
// URLs or opaque tokens.
function safeDetail(value: string): string {
  return value
    .replace(/\b(?:https?|wss?):\/\/[^\s'"<>]+/gi, '[url]')
    .replace(/\bBearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/\b(access_token|refresh_token|id_token|oauth_token|api_key|session_key|token|ticket|secret|password|authorization)\s*[:=]\s*\S+/gi, '$1=[redacted]')
    .replace(/\b[A-Za-z0-9_-]{40,}\b/g, '[redacted]')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 260)
}

export function describeRealtimeFailure(stage: RealtimeFailureStage, error: unknown): RealtimeFailure {
  if (error instanceof ConsoleContractError) {
    return {
      stage,
      code: 'console_contract_invalid',
      detail: 'stage=' + stage + ' path=' + safeDetail(error.path)
        + ' cause=' + safeDetail(error.message),
    }
  }

  if (error instanceof ConsoleHttpError) {
    const code = /^[A-Za-z0-9_.-]{1,80}$/.test(error.code) ? error.code : 'http_error'
    const endpoint = error.endpoint?.split('?')[0]
    return {
      stage,
      code,
      detail: [
        'stage=' + stage,
        'http=' + error.status,
        endpoint && /^\/[a-zA-Z0-9_/-]+$/.test(endpoint) ? 'endpoint=' + endpoint : '',
        error.elapsedMs !== undefined ? 'elapsed_ms=' + Math.round(error.elapsedMs) : '',
        'code=' + code,
      ].filter(Boolean).join(' '),
    }
  }

  if (error instanceof Error) {
    return {
      stage,
      code: error.name === 'SyntaxError' ? 'invalid_json' : 'realtime_exception',
      detail: 'stage=' + stage
        + ' type=' + safeDetail(error.name)
        + ' cause=' + safeDetail(error.message),
    }
  }

  return { stage, code: 'realtime_unknown_failure', detail: 'stage=' + stage + ' type=unknown' }
}
