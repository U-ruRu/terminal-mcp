# Connector runtime contract

Release: **0.14.2**. Contract: Distributed Multi-Issuer Access Mesh V2, endpoint version 1.

In 0.14.2 both Executor and Coordinator publish `session` with required `access_code` and attach-only `action`. Only Access MCP issues or ends a session. The optional `issuer_node_id` is required for unqualified four-digit access codes.

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

Only Access `session` accepts `start`, `end`, `status`. `start` requires `mode`; legacy issuance rejects a supplied code, and persistent activation requires an existing persistent slot's four-digit code. The issuer namespace scopes the code. Slot kind is immutable and has exactly two values: `legacy`, `persistent`.

Executor/Coordinator `session` is attach-only and accepts `attach`, an Access Code and its issuer. The qualification can be `issuer_node_id` plus `access_code`, or an `issuer:dddd` code. The same slot can attach to multiple role/node connectors. The trusted provider/principal/role context identifies each binding; an existing binding cannot switch slots. Subsequent domain writes omit codes and client-asserted identity/epoch fields. Ending a session is an Access/issuer operation, not a detach message to each node.

Local grant/policy replicas provide admission. Reads use a pure observation path. Writes materialize the local WorkSession and check its local deadline, epoch and cleanup fence. Local operations do not call an issuer RPC for each permit. A partition does not suspend already-replicated deadlines; unseen issuer changes become enforceable after delivery/catchup. Binding an unknown grant requires local replication first.

## Workflow-state and ownership contract

Task state is one of `ready`, `in_progress`, `blocked`, `deferred`, `done`. Create sets initial state; subsequent workflow transitions use `state` or `done`. `update` rejects a state field. Claim/release/expiry/end/suspend/delete preserve explicit task state, checkpoint and result.

Legacy claims release on end/expiry. Persistent claims release on end/expiry when their policy says `release_on_end=true`; otherwise ownership remains durable. Suspend/delete triggers cleanup. Local cleanup fences execution before releasing ownership and retains a pending fence on failure. Each release targets its exact old claim/session identity and is idempotent; a successor claim survives delayed cleanup.

Owner-sensitive task writes validate ownership in their write transaction, independently of revision checking. Revision checks protect policy/dependency/output preconditions. Executor task_comment accepts comment (default) and checkpoint; checkpoint retains canonical owner/revision guards. Comments retain independent append semantics. Review and its audit commit together. All successful mutation receipts are derived from the transaction's committed snapshot, including state, revision and requested description/checkpoint/result; post-commit readback does not decide whether the mutation succeeded.

## Planning and runtime validation

Planning inputs are **permissive planning schemas**: a flat callable surface, meaningful descriptions, action-field visibility and constraint annotations. The original strict request schema is retained under `x-runtime-schema` or equivalent per-field/action annotations. Unknown arguments, wrong types, missing required fields and cross-field violations reach runtime validation and return structured errors before mutation.

Task create and archive preserve both their ordinary fields and formal conditions. Create requires `namespace` and `isolation_hint`; `state=done` also requires a non-null result. Archive requires `namespace`, `task_id` and `archive_note` or `note`. Disallowed state/property combinations are not made valid by permissive planning.

Effective role inputs have schema-size tests. Most inputs are below 8 KiB. Coordinator task_manage retains the complete action schema under an explicit 40 KiB ceiling. Output planning is a flat/coarse object below 2 KiB; the authoritative output model remains strict. Contract baselines record planning/runtime digests, byte sizes and annotations.

## Wire errors, outcomes and replay

Handled application failure has MCP `isError:false`, JSON `ok:false`, and an `error` object. `structuredContent` and text fallback represent the same result. Recovery uses the actual `code`, `details`, `outcome`, `retry`, `reason` and `path` returned by the server.

A validation or fenced-owner failure has no mutation effect. A network delivery failure after local message acceptance can have `outcome=committed`: retain the message hash and let its outbox retry. A successfully committed task returns the receipt captured inside its transaction even if a subsequent reader/projection fails. Unknown outcomes require persisted-state reconciliation before another mutation.

Automatic task replay includes normalized payload, trusted principal/role, LogicalAgent, WorkSession/epoch and server-observed MCP request ID. A reused zero, empty or nonzero ID does not collapse different task requests. An exact retry returns its durable original result; a successor session or different payload has a distinct identity. The current gate is evaluated before serving a task replay. No public role-tool idempotency argument is added.

Shell launch requires stronger care: constant JSON-RPC ID `0` is treated as a fresh launch, while unique server-observed operation IDs support durable reservation/replay. Do not infer exactly-once shell effects from a constant ID. Read `cmd_hash` or the local journal after an uncertain launch.

## Read paths and paging

`command_read(cmd_hash=...)` reads retained local output for any LogicalAgent without Access Code or prior attach. `command_read` with no hash returns a bounded all-agent local journal with command and identity metadata. Transport authentication still applies. Coordinator health also works before attach; health collection success and actual component health are separate fields.

Task reads and message reads use their declared scope and canonical projections. Message inbox/history/ACK are local to the attached connector's execution node; they do not query the issuer for read permission. Opaque cursors bind the original query/caller where required. Reuse them unchanged, with the same filters/detail mode. Message paging uses durable keyset sequences and is safe while prior inbox rows are consumed or retained data is pruned.

Output budgets apply to encoded bytes, not just text length. Oversized full messages remain unacknowledged; summary mode provides a bounded alternative. Response preflight succeeds before notify surfacing changes read state.

## Message delivery and obligations

Both roles support recipients/send/read/ack/reply/history with `scope=local|fleet`; default is fleet. Sender identity is resolved from local trusted binding. Broadcast omits target or uses `broadcast`; a task recipient set uses namespace plus task_id. Discovery exposes active unique public names and safe lifecycle/activity metadata. Sender, duplicate LogicalAgent and expired local recipients are excluded.

Local recipients, message metadata, obligations and pending peer jobs commit together before network forwarding. Local-only delivery sends no peer request. Fleet forwarding uses authenticated peers and an idempotent durable outbox; total synchronous forwarding wait is bounded, including lock wait. States `queued` and `partial` preserve the true local acceptance outcome rather than claiming remote delivery.

Notify read acknowledges only successfully surfaced rows. Mode ack requires ACK; alert/require_reply requires a reply and remains an obligation after ACK. Reply creation, local obligation release and pending receipt/delivery jobs are atomic. Parent-message routing brings replies to the original sender's execution node. Restart/lost-ACK retries preserve one accepted message effect.

The full peer payload, including broadcast exclusions, and the full acceptance proof must fit the 64 KiB Fleet message budget before commit. A payload that cannot ever be acknowledged is rejected transactionally.

## Operator and mobile contract

Mobile/Console calls the operator API for the same two immutable slot kinds. Read endpoints are `/actions/access/slots`, `/actions/access/slots/{slot_id}`, `/actions/access/defaults`. `POST /actions/access/mutate` supports create/defaults/policy/deadline/end/suspend/resume/rotate/delete. This operator HTTP surface uses explicit idempotency keys and revision guards; it exposes no command execution.

Duration, cooldown, rearm, warning/draining and release-on-end are per-slot policy. Warning/draining thresholds must be nonnegative and smaller than duration. Policy defaults persist separately and affect subsequent issuance defaults. A deadline mutation applies to an active cycle and requires a timezone-aware time later than its start. Slot list views exclude Access Codes; code issuance/rotation receipts are sensitive.

## Effect metadata

MCP annotations describe the most consequential allowed action; `x-terminal-mcp-action-matrix` details individual actions. Role attach is non-destructive and idempotent. Access combines issuance/status/end; aggregate issuance is not idempotent. Task create with generated identity is not generally idempotent. Command run/recovery is destructive, open-world and non-idempotent; cancel is destructive/idempotent. Inbox read has surfacing effects, unlike pure history or command output reads.

OpenAPI operations publish corresponding `x-openai-isConsequential` classification. Metadata never grants authorization and cannot replace runtime capability checks.

## Release verification

Run focused/runtime/discovery/documentation tests, the complete regression suite, and live FirstByte/BacLOUD checks on the exact release SHA. Required boundaries are six primary connectors, both directions of messaging, task ownership lifecycle, issuer-partition local operations, code-free cross-agent command reads and mobile revocation/deadline controls. See [deployment and acceptance](access-mesh-deployment.md). Legacy `/mcp` checks complement this matrix; they do not substitute for it.

In 0.14.2 directed Fleet messages addressed by public_name are delivered to each active node attachment of the recipient LogicalAgent; local-first broadcast still deduplicates the recipient globally across nodes. Each local inbox deduplicates delivery by message_hash.
