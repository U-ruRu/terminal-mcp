# Architecture

## Layers

`terminal_mcp.core` owns command orchestration, Agent Session policy, coordination messages and queue selection. `terminal_mcp.storage` owns durable SQLite state, the disposable output-cache and atomic command transitions. MCP and HTTP are thin transports over one `TerminalService`. `terminal_mcp.terminal` owns process spawning, bounded output capture and worker execution.

## Agent policy

`AgentPolicy` is injected from `Settings`. Defaults are: idle TTL 300 seconds, intent lease 180 seconds, hard session lifetime 1500 seconds, non-blocking warning after 1200 seconds, blocking session ALERT after 1380 seconds with 60-second repeat interval, message reminder window 180 seconds, five reminder responses, command preview 160 characters and eight compact active peers.

The hard session deadline is measured from immutable `registered_at`. Activity refreshes idle liveness, while `coordinate(step + intent)` refreshes the intent lease. The hard deadline is never extended. Derived public statuses are `started`, `active`, `idle`, `finished` and `forced`. Forced sessions persist an `end_reason` such as `idle_timeout` or `max_session_duration`.

Every agent-bound operational response carries session timing. After the configured warning threshold it instructs the agent to reach a safe checkpoint and return to chat. After the configured session-alert threshold the coordinator creates a normal persisted `ALERT`; it blocks normal work until reply, then becomes eligible to appear again after `session_alert_repeat_seconds` while the same session remains alive. Hard expiry still wins at `max_session_seconds`. Already queued or running terminal commands are independent from Agent Session lifetime.

## Agent gate

`AgentCoordinator.gate` centralizes lifecycle, intent and message policy. `run` requires a live session, fresh intent, explicit acknowledgement of unread messages and completion of required replies. An `ALERT` additionally blocks the normal work surface (`run`, `read`, `coordinate`, `agents`, plan update). `message`, `health`, `cancel`, `recovery` and `agent_finish` remain available so an agent can respond, inspect health, stop work or use emergency execution.

## Coordination messages

Messages use a recipient state machine persisted in SQLite:

`delivered -> seen -> read -> replied`

`seen` means Terminal MCP surfaced the message in a tool response. `read` requires an explicit `message(agent_id, message_hash=...)` acknowledgement. A seen but unacknowledged message keeps its full text visible for at least 180 seconds and at least five surfaced responses; afterward it remains as a compact reminder until acknowledgement.

`require_reply=true` keeps `run` blocked after acknowledgement until the recipient sends `message(agent_id, message_hash=..., text=...)`. `alert=true` implies a required reply and is rendered as `ALERT` in every permitted agent-bound response until replied. Automatic session ALERTs are ordinary persisted alerts from the system sender and can therefore be acknowledged/replied through the same API; a later alert is created after the configured repeat interval if the session continues. Broadcast messages snapshot recipients at send time and keep per-recipient receipts.

## Numbered execution queues

Normal `run` execution uses stable numbered FIFO lanes. SQLite is the queue source of truth; Python does not own a canonical deque. Each lane has one worker and different lanes can execute concurrently.

A queued command stores `queue_id`, `queue_sequence` and `enqueued_at`. A worker atomically claims the oldest queued row for its lane using `queued -> running`, recording `claimed_at`. Completion and cancellation use guarded transitions from the expected current state. This prevents stale in-memory command objects from restoring an obsolete `queued` or `running` state.

Agent sessions store only `preferred_queue_id`. The first `run` without an explicit queue selects the least-loaded lane. Later calls reuse the preferred lane. An explicit `queue_id` routes the command there and updates affinity. Queue workers and commands remain valid after the owning Agent Session finishes or expires.

Workers reconcile SQLite periodically in addition to wake-up events, so a persisted queued row is eventually claimed even if an event is lost. `recovery` remains a separate immediate execution path outside numbered queues.

## Observation and journals

`agents()` is also an anonymous read-only observer. It can return recent sessions without creating an Agent Session. `target` selects a public agent name and returns its last activity tool plus the persisted recipient message journal with `delivered`, `seen`, `read`, and `replied` states. `show_details`, `show_intents`, and `show_commands` expand the current plan, the persisted intent journal and the command journal. `command_hash` returns the full original command metadata, while output remains the responsibility of `read`. `since_minutes` provides a relative history window.

Durable SQLite persists sessions, task events, activity, command attribution, queue metadata, messages and receipts. Schema v5 migrates legacy output from the durable `lines` table into a separate disposable output-cache, removes both legacy line indexes (including the duplicate `idx_lines_hash_seq`), drops `lines`, then compacts durable SQLite. Legacy `expired` lifecycle rows are normalized to `forced`; legacy sessions that cannot satisfy the current plan schema receive an explicit forced end reason.

## Bounded output persistence

Output cache uses one canonical `(hash, seq)` index. A logical line is capped at 4 MiB and persisted output per command at 8 MiB. Writes are batched instead of committing every line. Cache retention uses 192 MiB target, 256 MiB hard ceiling and one million rows; when a ceiling is reached, complete outputs of the oldest non-running commands are evicted until the target is satisfied. Durable command metadata records whether output was truncated or pruned. `health` exposes cache usage and prune statistics.
