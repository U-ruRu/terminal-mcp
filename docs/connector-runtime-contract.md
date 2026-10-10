# Connector runtime contract

Release: **0.14.4**. Contract: Distributed Multi-Issuer Access Mesh V2, endpoint version 1.

In 0.14.4 both Executor and Coordinator publish `session` with required `session_number` and attach-only `action`. Only Access MCP issues or ends a session. The optional `issuer_node_id` is required for unqualified four-digit session numbers.

## Published surfaces

Access endpoint: `/terminal-mcp/access/v1/mcp`.

Access catalog: `session`.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Both FirstByte and BacLOUD publish all three surfaces. Legacy compatibility endpoint `/mcp` retains its own behavior:

Legacy compatibility catalog: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

The effective V2 discovery authority is `src/terminal_mcp/mcp/access_mesh_schema_baselines_v2.json`, generated from mesh-enabled tools rather than old bootstrap models.

## Issuer session versus role attach

Only Access `session` accepts `start` and `end`. Public start takes no mode or session number and returns only `ok: true` with the four-digit `session_number`; end returns only `ok: true`. Number availability is negotiated across reachable Mesh peers, and the local issuer remains available during partitions. The operator provisions persistent slots through HTTP/Console.

Executor/Coordinator `session` is attach-only and accepts a four-digit `session_number` without a public issuer selector. Mesh resolves the source and current LogicalAgent. The same session may attach to multiple role/node connectors. The trusted provider/principal/role context identifies each binding; an existing binding cannot switch numbers. Subsequent domain writes omit codes and client-asserted identity/epoch fields. Ending a session is an Access/issuer operation, not a detach message to each node.

Local grant/policy replicas provide admission. Reads use a pure observation path. Writes materialize the local WorkSession and check its local deadline, epoch and cleanup fence. Local operations do not call an issuer RPC for each permit. A partition does not suspend already-replicated deadlines; unseen issuer changes become enforceable after delivery/catchup. Binding an unknown grant requires local replication first.

## Workflow-state and ownership contract

Task state is one of `ready`, `in_progress`, `qa`, `blocked`, `deferred`, `done`. Create sets initial state. `task_state` and `task_manage(action="state")` accept any listed state from any admitted agent at any time, independent of claim ownership, prior state, result and dependency status. The `task_state` input contains `namespace`, `task_id` and `state`; each state write preserves the content revision and repeated values preserve that revision. `update` rejects a state field. Claim/release/expiry/end/suspend/delete preserve explicit task state, checkpoint and result.

Legacy claims release on end/expiry. Persistent claims release on end/expiry when their policy says `release_on_end=true`; otherwise ownership remains durable. Suspend/delete triggers cleanup. Local cleanup fences execution before releasing ownership and retains a pending fence on failure. Each release targets its exact old claim/session identity and is idempotent; a successor claim survives delayed cleanup.

Content edits preserve the task revision/CAS contract. Checkpoints append history and update the latest checkpoint while preserving content revision; repeated checkpoint values append separate events. Comments retain author attribution and history, with exact-text duplicate detection per author. Executor `task_comment` accepts `comment` (default) and `checkpoint`. Review and its audit commit together. Successful mutation receipts come from the transaction's committed snapshot, including state, revision and requested description/checkpoint/result; the committed outcome remains authoritative through post-commit readback failures.

The action-specific runtime schema exposes `request_id` for `task.create`. Its receipt is scoped by LogicalAgent, namespace and operation ID, with a content fingerprint. Reuse with changed content returns `request_id_conflict`; a fresh ID represents a separate create operation. JSON-RPC/MCP request IDs identify transport calls. Repeated checkpoint calls append independent events and retain their authorship records.

## Planning and runtime validation

Planning inputs are **permissive planning schemas**: a flat callable surface, meaningful descriptions, action-field visibility and constraint annotations. The original strict request schema is retained under `x-runtime-schema` or equivalent per-field/action annotations. Unknown arguments, wrong types, missing required fields and cross-field violations reach runtime validation and return structured errors before mutation.

Task create and archive preserve both their ordinary fields and formal conditions. Create requires `namespace` and `isolation_hint`; `state=done` also requires a non-null result. Archive requires `namespace`, `task_id` and `archive_note` or `note`. Disallowed state/property combinations are not made valid by permissive planning.

Effective role inputs have schema-size tests. Most inputs are below 8 KiB. Coordinator task_manage retains the complete action schema under an explicit 40 KiB ceiling. Output planning is a flat/coarse object below 2 KiB; the authoritative output model remains strict. Contract baselines record planning/runtime digests, byte sizes and annotations.

## Wire errors, outcomes and replay

Handled application failure has MCP `isError:false`, JSON `ok:false`, and an `error` object. `structuredContent` and text fallback represent the same result. Recovery uses the actual `code`, `details`, `outcome`, `retry`, `reason` and `path` returned by the server.

A validation or fenced-owner failure has no mutation effect. Local message acceptance has no network forwarding phase: a successful message result reflects committed local recipients, while an unavailable directed local recipient fails before message creation. A successfully committed task returns the receipt captured inside its transaction even if a subsequent reader/projection fails. Unknown outcomes for other mutation classes require persisted-state reconciliation before another mutation.

Automatic task replay includes normalized payload, trusted principal/role, LogicalAgent, WorkSession/epoch and server-observed MCP request ID. A reused zero, empty or nonzero ID does not collapse different task requests. An exact retry returns its durable original result; a successor session or different payload has a distinct identity. The current gate is evaluated before serving a task replay. No public role-tool idempotency argument is added.

Shell launch requires stronger care: constant JSON-RPC ID `0` is treated as a fresh launch, while unique server-observed operation IDs support durable reservation/replay. Do not infer exactly-once shell effects from a constant ID. Read `cmd_hash` or the local journal after an uncertain launch.

## Read paths and paging

`command_read(cmd_hash=...)` reads retained local output for any LogicalAgent without Session Number or prior attach. `command_read` with no hash returns a bounded all-agent local journal with command and identity metadata. Transport authentication still applies. Coordinator health also works before attach; health collection success and actual component health are separate fields.

Task reads and message reads use their declared scope and canonical projections. Message inbox/history/ACK are local to the attached connector's execution node; they do not query the issuer for read permission. Opaque cursors bind the original query/caller where required. Reuse them unchanged, with the same filters/detail mode. Message paging uses durable keyset sequences and is safe while prior inbox rows are consumed or retained data is pruned.

Output budgets apply to encoded bytes, not just text length. Oversized full messages remain unacknowledged; summary mode provides a bounded alternative. Response preflight succeeds before notify surfacing changes read state.

## Message delivery and obligations

Both roles support recipients/send/read/ack/reply/history on the server handling the connector call. scope=local is the default; the legacy scope=fleet value remains parseable for compatibility but returns feature_disabled without mutation. Sender identity is resolved from the local trusted binding. Broadcast omits target or uses broadcast; a task recipient set uses namespace plus task_id. Discovery exposes active unique public names with a current local attachment and excludes the sender and expired local sessions.

Local recipients, message metadata and obligations commit atomically without message peer forwarding. ACK is idempotent; replies are new local messages linked by reply_to. Retention is exactly 24 hours from each message original created_at; reads and ACKs do not extend it, while a reply has its own 24-hour lifetime. Expired rows stop creating command-gate obligations immediately and are physically pruned by the running message worker. Legacy `:meshmsg:` rows are ignored by the new local protocol (no migration), and only `:localmsg:` records are subject to the 24-hour new-message cleanup. Unacknowledged legacy message/receipt jobs retain their original attempts and a final diagnostic reason, without being marked delivered or retried after restart. Physical deletion of old junk is a separate operator decision.

Notify read acknowledges only successfully surfaced rows. Mode ack requires ACK; alert/require_reply requires a reply and remains an obligation after ACK. Reply creation and local obligation release are atomic and create no peer receipt/delivery job. Old legacy message IDs return `message_not_found` for ACK/reply and never create new messages. New local replies need their original author to be an active local recipient, and each reply has its own lifetime. Restart preserves accepted unexpired local history and keeps terminal legacy jobs stopped.

The full peer payload, including broadcast exclusions, and the full acceptance proof must fit the 64 KiB Fleet message budget before commit. A payload that cannot ever be acknowledged is rejected transactionally.

## Operator and mobile contract

Mobile/Console calls the operator API for the same two immutable slot kinds. Read endpoints are `/actions/access/slots`, `/actions/access/slots/{slot_id}`, `/actions/access/defaults`. `POST /actions/access/mutate` supports create/defaults/policy/deadline/end/suspend/resume/rotate/delete. This operator HTTP surface uses explicit idempotency keys and revision guards; it exposes no command execution.

Duration, cooldown, rearm, warning/draining and release-on-end are per-slot policy. Warning/draining thresholds must be nonnegative and smaller than duration. Policy defaults persist separately and affect subsequent issuance defaults. A deadline mutation applies to an active cycle and requires a timezone-aware time later than its start. Slot list views exclude Session Numbers; session number issuance/rotation receipts are sensitive.

## Effect metadata

MCP annotations describe the most consequential allowed action; `x-terminal-mcp-action-matrix` details individual actions. Role attach is non-destructive and idempotent. Access combines issuance/status/end; aggregate issuance is not idempotent. Task create with generated identity is not generally idempotent. Command run/recovery is destructive, open-world and non-idempotent; cancel is destructive/idempotent. Inbox read has surfacing effects, unlike pure history or command output reads.

OpenAPI operations publish corresponding `x-openai-isConsequential` classification. Metadata never grants authorization and cannot replace runtime capability checks.

## Release verification

Run focused/runtime/discovery/documentation tests, the complete regression suite, and live FirstByte/BacLOUD checks on the exact release SHA. Required boundaries are six primary connectors, both directions of messaging, task ownership lifecycle, issuer-partition local operations, code-free cross-agent command reads and mobile revocation/deadline controls. See [deployment and acceptance](access-mesh-deployment.md). Legacy `/mcp` checks complement this matrix; they do not substitute for it.

In 0.14.4 directed Fleet messages addressed by public_name are delivered to each active node attachment of the recipient LogicalAgent; local-first broadcast still deduplicates the recipient globally across nodes. Each local inbox deduplicates delivery by message_hash.
