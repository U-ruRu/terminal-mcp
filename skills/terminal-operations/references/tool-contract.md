# Terminal MCP tool contract

Version: **0.13.1**.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Role v1 uses server-resolved identity and the shared Application API. Inputs are permissive planning schemas; authoritative runtime validation precedes every operation. Output planning is a flat object with named success fields, `ok` and `error`; strict wire models remain server-side.

Handled application errors use MCP `isError: false` with `structuredContent.ok: false` and a machine-readable `error` object. Bounded collections use opaque `next_cursor` values.

Bootstrap-created managed sessions and temporary legacy sessions release task claims on end, interrupt and expiry. Explicitly provisioned persistent slots retain their separate ownership policy. Checkpoints and task history survive session cleanup.


## Session and identity

Call `session(action="start")` before managed operations. Identity, principal, role and session epoch come from server request context. Repeated starts preserve the active session. Each WorkSession fixes the endpoint role and contract version. A role handoff ends the old session and starts the next role with the appropriate provider identity.

Provider metadata and authorization are separate. Equal supported identity evidence resolves to the same LogicalAgent; distinct connector metadata stays distinct. `command_read` by known `cmd_hash` remains available without an Access Code.

## Executor workflow

`task_list` returns bounded TaskListItem records. `task_claim(action="claim", namespace=..., task_id=..., claim_intent=...)` returns TaskWorkingSet. Claiming ready work atomically moves it to `in_progress`. `task_state` applies the shared state machine; `task_comment` appends history. Release uses `task_claim(action="release", ...)`.

`command_run` accepts `command`, optional `queue_id` and `task_scope`. Numbered queues execute FIFO. A fast completion includes the first output page; queued/running receipts contain `cmd_hash`. Continue with `command_read`, `limit` and the returned opaque cursor. `command_cancel` cancels an owned command; `command_recovery` uses the managed recovery path.

## Coordinator workflow

`task_get` selects `snapshot`, `detail` or one paginated history stream. `task_graph` returns bounded nodes and edges. `task_manage` creates, updates, checkpoints, reviews, relates, archives and completes tasks under the shared ownership and revision rules. Read back a mutation before continuing after an uncertain outcome.

`agent_observe` returns LogicalAgent, WorkWindow and WorkSession. `health` works before session start; default output is compact, `extended=true` adds bounded diagnostics. `ok` confirms diagnostic collection, while `healthy`, `status` and components describe service health.

## Tasks and lifetime

States: `ready`, `in_progress`, `blocked`, `deferred`, `done`.
Lanes: `implementation`, `review`, `release`, `integration`, `general`.
Priorities: `P0`, `P1`, `P2`, `P3`.

Task checkpoints, descriptions, results, comments and reviews are durable native JSON records. Bootstrap-managed and temporary legacy claims belong to the creating WorkSession lifetime and are released on end, interrupt or expiry. Explicit persistent-slot ownership follows its own policy. Session cleanup preserves task history and permits the next agent to claim available work. Old session IDs and epochs cannot mutate current ownership.

## Messaging

Both roles expose `message`: `recipients`, `send`, `read`, `ack`, `reply`, `history`. Use the discovered `public_name` as target. Sender identity is server-resolved. Broadcast uses `target="broadcast"` or an omitted target; task addressing uses `namespace` and `task_id`. Required acknowledgements and alerts participate in the managed execution gate. Read/ACK/reply/history use the returned `message_hash`.

## Input, output and recovery

Planning inputs expose callable fields. Runtime validation checks required fields, types, bounds and action prerequisites before mutation. Output planning exposes named top-level fields without references or structural unions; runtime wire schemas stay strict. Per-tool input and output byte budgets prevent schema growth.

Handled application failures use `isError:false` and `{"ok":false,"error":{...}}`. Inspect `error.code`, `details`, `outcome`, `retry`, `reason` and `path`. Repair arguments, restart a session or reconcile committed state according to the recovery fields. The JSON text fallback represents the same object.

Task revisions protect optimistic concurrency. Command replay requires unique server-observed operation IDs; the client adds no public idempotency argument. Reused JSON-RPC ID `0` represents fresh command launches. Exact task retries use a normalized mutation fingerprint. A constant transport ID cannot guarantee exactly-once shell execution.

## Operation metadata

Read-only task/command observation and health remain read-only. Session, task and message tools advertise their aggregate mutation effects. Shell run/recovery is destructive and open-world; cancel is destructive and idempotent. Mixed-action tools use the most consequential permitted action. OpenAPI classifies each published operation separately, including read-only POST operations. Metadata is independent of authorization.

## Legacy compatibility

Endpoint: `/mcp`.

Legacy compatibility catalog: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

This adapter uses the same Application API and persisted records. Existing Access Codes support explicitly authorized compatibility operations. New connector workflows use the versioned role endpoints. The legacy catalog is retained for existing deployments; it is outside the primary FirstByte/BacLOUD connector acceptance surface.
