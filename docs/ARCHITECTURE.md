# Architecture

## Layers

`terminal_mcp.core` owns command orchestration, Agent Session policy, coordination messages, optional managed-task coordination and queue selection. `terminal_mcp.storage` owns durable SQLite state, managed-task persistence, the disposable output-cache and atomic command transitions. MCP and HTTP are thin transports over one `TerminalService`. `terminal_mcp.terminal` owns process spawning, bounded output capture and worker execution.

## Agent policy

`AgentPolicy` is injected from `Settings`. Defaults are: idle TTL 300 seconds, task-context TTL 180 seconds, hard session lifetime 1500 seconds, non-blocking warning after 1200 seconds, blocking session ALERT after 1380 seconds with 60-second repeat interval, message reminder window 180 seconds, five reminder responses, command preview 160 characters and eight compact active peers.

The hard session deadline is measured from immutable `registered_at`. Activity refreshes idle liveness, while `coordinate(step + intent)` refreshes task-context freshness. Canonical public names are `task_context_ttl_seconds` and `task_context_age_seconds`; `task_lease_seconds`, `task_age_seconds` and `max_task_age_seconds` remain compatibility aliases. The hard deadline is never extended. Derived public statuses are `started`, `active`, `idle`, `finished` and `forced`. Forced sessions persist an `end_reason` such as `idle_timeout` or `max_session_duration`.

Every agent-bound operational response carries session timing. After the configured warning threshold it instructs the agent to reach a safe checkpoint and return to chat. After the configured session-alert threshold the coordinator creates a normal persisted `ALERT`; it blocks normal work until reply, then becomes eligible to appear again after `session_alert_repeat_seconds` while the same session remains alive. Hard expiry still wins at `max_session_seconds`. Already queued or running terminal commands are independent from Agent Session lifetime.

## Agent gate

`AgentCoordinator.gate` centralizes lifecycle, intent and message policy. `run` requires a live session, fresh intent, explicit acknowledgement of unread messages and completion of required replies. An `ALERT` additionally blocks the normal work surface (`run`, `read`, `coordinate`, `agents`, plan update). `message`, `health`, `cancel`, `recovery` and `agent_finish` remain available so an agent can respond, inspect health, stop work or use emergency execution.

## Coordination messages

Messages use a recipient state machine persisted in SQLite:

`delivered -> seen -> read -> replied`

`seen` means Terminal MCP surfaced the message in a tool response and is not acknowledgement. Until explicit `message(agent_id, message_hash=...)`, the recipient is `ACK REQUIRED`; that call moves the receipt to `read`. A seen but unacknowledged message keeps its full text visible for at least 180 seconds and at least five surfaced responses; afterward it remains as a compact reminder until acknowledgement.

`require_reply=true` keeps `run` blocked after acknowledgement until the recipient sends `message(agent_id, message_hash=..., text=...)`. `alert=true` implies a required reply and is rendered as `ALERT` in every permitted agent-bound response until replied. Automatic session ALERTs are ordinary persisted alerts from the system sender and can therefore be acknowledged/replied through the same API; a later alert is created after the configured repeat interval if the session continues. Broadcast messages (omitted target or explicit `target="broadcast"`) snapshot recipients at send time and keep per-recipient receipts. После normal `agent_finish` exact old `agent_id` имеет 300-second receipt grace только для ACK/reply конкретного уже delivered message hash. Receipt state and recipient availability are separate: sender inspection exposes ended recipients through `inactive_recipients`. Finishing with outstanding obligations is allowed and returns `pending_communication` with `unacknowledged`, `reply_required` and `alerts` when present. A send with `namespace + task_id` addresses the managed task: current live claimants are snapshotted as recipients while the message reference and body are also appended to durable task history, preserving the coordination note across claimant turnover.

## Numbered execution queues

Normal `run` execution uses stable numbered FIFO lanes. SQLite is the queue source of truth; Python does not own a canonical deque. Each lane has one worker and different lanes can execute concurrently.

A queued command stores `queue_id`, `queue_sequence` and `enqueued_at`. A worker atomically claims the oldest queued row for its lane using `queued -> running`, recording `claimed_at`. Completion and cancellation use guarded transitions from the expected current state. This prevents stale in-memory command objects from restoring an obsolete `queued` or `running` state.

Agent sessions store only `preferred_queue_id`. The first `run` without an explicit queue selects the least-loaded lane. Later calls reuse the preferred lane. An explicit `queue_id` routes the command there and updates affinity. Queue workers and commands remain valid after the owning Agent Session finishes or expires. Task provenance is explicit: every normal `run` requires `task_scope` before enqueue. With no live claims the only legal value is `none`; with live claims legal values are `none|all|namespace/task_id` and are exposed as `task_scope_options`. Only the selected scope receives command events.

Workers reconcile SQLite periodically in addition to wake-up events, so a persisted queued row is eventually claimed even if an event is lost. `recovery` remains a separate immediate execution path outside numbered queues.

## Managed task coordination

Managed tasks are optional durable coordination state layered over Agent Sessions. Ad-hoc server work requires no task. Every task has a required namespace, one fixed lane and workflow state `ready`, `blocked`, `deferred` or `done`. `done` means the task goal and acceptance criteria were achieved and therefore requires a meaningful durable result.

Archive is an independent lifecycle dimension. `archive` requires `archive_note`, records `archived_at` and audit evidence, atomically releases live claims and removes the task from active backlog/pressure/recommendation while preserving workflow state/history. Archived done stays completed; archived unfinished stays unfinished. Draft v9 migrates production v8 `state=archived` deterministically, using event evidence to recover prior workflow state when possible and retaining migration provenance for safe fallback.

Claims are the source of truth for ownership. `cooperative` controls concurrent participation rather than task visibility. The earliest live claim is derived owner; later live claims on `cooperative=true` tasks are participants. `cooperative=false` permits one live claim. Releasing/finishing/expiring the owner transfers ownership to the earliest remaining live claim. Primary claim requires short durable `claim_intent`; repeat claim by the same agent updates intent without reclaim. Observation exposes claimed_at, claim_age_seconds, claim_intent and owner/participant role; no separate mutable owner state is required.

Owner-only operations include checkpoint, workflow state transitions, done/blocked, dependency mutations and cooperative changes affecting ownership invariants. Participants retain comments, task-scoped commands and centrally defined safe-metadata changes. `cooperative=true -> false` is rejected while multiple live claims exist. Claimed blocked requires `blocker_reason`, claimed done requires result, release requires `release_reason`, retained as durable handoff history; state/claim mutation and audit evidence must be transactionally consistent.

Append-only task events implement universal comments/history through `action=comment` + `comment_text`. Description is mutable current work description; comments/events are chronology and survive release, session end, archive and restart. Generic relations use `action=relate|unrelate` with `relation_kind` + related namespace/task id. Relation views expose direction, kind, namespace, task_id and created_at. Review is the first consumer using kind `review_of`: a lane=review task finishes through done(result) or records blocking findings/comments and becomes blocked. Completion/blocking emits linked `review_feedback` history on the reviewed task with review reference, outcome, author/timestamp, candidate_ref and result or blocker findings. Legacy work_reviews remains read-only historical compatibility.

Dependencies form a validated directed prerequisite graph. Self edges and cycles are rejected before persistence. Forward references to missing tasks are allowed, represented as status `missing` and remain blocking/observable. A dependency is satisfied only by successful completion semantics independent of archive visibility: archived done is satisfied, archived unfinished remains blocking. `force=true` + non-empty `force_reason` is a durably audited conscious override of dependency claim gate only.

Storage transactions couple domain state with promised audit evidence: dependency override with its override event; claim/release with ownership projection; done/blocked with result/reason history; archive with lifecycle evidence and claim release; relation/review feedback with linked history; and queued command submission with explicitly selected task provenance. A failed compound operation must not leave a success-looking partial history.

The task store persists work items, claims, dependencies, generic relations, legacy reviews and append-only events in durable SQLite, separate from output cache. Tasks durably store tags, result, state timestamps, archive lifecycle metadata and creator-supplied `isolation_hint`. The hint is required on create, capped at 160 characters, exposed in task cards and live claimant task context, and remains domain-agnostic data: no Git/worktree inference, recommendation or execution gate is derived from it. Pressure/recommendation use one claimability predicate, expose raw claimable count plus weighted routing pressure, and order equal-priority READY work by oldest ready_since; diagnostics expose oldest claimable ready and missing dependency count. Finishing Agent Session releases live claims while retaining durable history. `run.task_scope` controls command attribution independently from claim ownership/intent.

`tasks` is compact observation; `task` is explicit mutation. MCP and OpenAPI Actions are thin transports over the same service/domain implementation, including owner authorization, graph validation and atomic audit guarantees.

## Observation and journals

`agents()` is also an anonymous read-only observer. Compact output is the default: enough session/fleet state for the next coordination decision, with task references when present. `target` selects one public agent. `show_details`, `show_intents`, and `show_commands` expand plans and journals only when requested; `command_hash` returns the full original command metadata, while output remains the responsibility of `read`. `since_minutes` provides a relative history window. Normal `run`/`read` success responses avoid repeating fleet/session context and attach it when warnings, messages, alerts or other exceptional coordination state make it actionable.

Durable SQLite persists sessions, task events, activity, command attribution, queue metadata, messages, receipts and managed task state. Schema v10 adds durable `isolation_hint` to managed tasks and migrates legacy rows to `none`; schema v9 adds durable result/tags/state timestamps, separate archive lifecycle metadata, claim intent, generic task relations and append-only comment/history semantics; it deterministically upgrades production v8 archived rows and preserves legacy review records; schema v8 widens the managed-task state constraint to include `archived` using a data-preserving `work_items` rebuild with foreign-key verification; schema v7 adds task-addressed message metadata/indexing on top of the v6 task registry; schema v6 adds the task registry on top of the v5 output-cache migration; schema v5 migrates legacy output from the durable `lines` table into a separate disposable output-cache, removes both legacy line indexes (including the duplicate `idx_lines_hash_seq`), drops `lines`, then compacts durable SQLite. Legacy `expired` lifecycle rows are normalized to `forced`; legacy sessions that cannot satisfy the current plan schema receive an explicit forced end reason.

## Bounded output persistence

Output cache uses one canonical `(hash, seq)` index. A logical line is capped at 4 MiB and persisted output per command at 8 MiB. Writes are batched instead of committing every line. Cache retention uses 192 MiB byte target, 256 MiB hard ceiling and one million rows. Byte pressure keeps the existing byte target semantics. Line pressure evicts complete outputs of the oldest non-running commands to create roughly 100,000 rows of headroom at the default limit (about 900,000 retained rows), with proportional batches for small configured limits. Durable command metadata records whether output was truncated or pruned. `health` exposes cache usage and prune statistics.
