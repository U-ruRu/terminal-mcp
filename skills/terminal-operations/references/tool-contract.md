# Terminal MCP tool contract

Version: **0.14.3**. Access Mesh V2. Both Executor and Coordinator `session` tools require `access_code` and permit only `action=attach`. The examples use synthetic code `0427`; substitute the code issued privately by Access.

Access endpoint: `/terminal-mcp/access/v1/mcp`.

Access catalog: `session`.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

FirstByte and BacLOUD each expose these three connectors. Primary role session is attach-only. Slot kinds: `legacy`, `persistent`; kind is immutable. Mobile/Console is an operator interface for those kinds.

## Access and attach examples

Create a fresh legacy slot on the chosen issuer:

<!-- contract-example: access.session -->
```json
{"action":"start","mode":"legacy"}
```

Activate an operator-provisioned persistent slot on its issuer:

<!-- contract-example: access.session -->
```json
{"action":"start","mode":"persistent","code":"0427"}
```

Issuer lifecycle status/end uses the Access binding or the issuer's code:

<!-- contract-example: access.session -->
```json
{"action":"status"}
```

<!-- contract-example: access.session -->
```json
{"action":"end"}
```

Bind each required Executor/Coordinator once:

<!-- contract-example: role.session -->
```json
{"action":"attach","issuer_node_id":"firstbyte","access_code":"0427"}
```

<!-- contract-example: role.session -->
```json
{"action":"attach","access_code":"firstbyte:0427"}
```

An unqualified four-digit code requires issuer_node_id. Initial attach is write-once for a connector binding and idempotent for the same slot. Rebinding to a different slot is a structured conflict. Subsequent role operations omit Access Codes and client-selected identity, provider metadata and epochs. Access handles issuer end; role tools do not expose detach or local session start/end.

## Local lifecycle and policy

A replicated grant retains LogicalAgent/public_name across roles and nodes. A local WorkSession/epoch and fixed deadline authorize writes on each execution server. Per-call admission uses local grant/policy/time; issuer RPC is not a read/write prerequisite. Unseen events require network recovery/catchup; known deadlines continue to apply through a partition.

Policy contains duration_seconds, cooldown_seconds, rearm_enabled, warning_seconds, draining_seconds, release_on_end. Local expiry/rearm follows the policy without repeated attach. Warning/draining thresholds are smaller than duration. Pending cleanup keeps a write fence until old execution is drained and applicable claims are released. Legacy releases at end/expiry; persistent retains ownership unless release_on_end=true. Suspend/delete triggers revocation cleanup.

Claim/release/lifecycle cleanup preserve explicit state, checkpoint and result. States are ready/in_progress/blocked/deferred/done. Create sets initial state; state/done explicitly changes it. Property update preserves state and rejects a state argument. Safe successor claims survive late cleanup through exact claim/session fences.

## Executor operations

`task_list` returns TaskListItem records. `task_claim` accepts claim/release; claim requires claim_intent. `task_state` explicitly transitions workflow and supplies result when completing or blocker_reason when entering an owned blocked state. `task_comment` supports `action=comment` (the default) for appended history and `action=checkpoint` for a durable checkpoint. Checkpoint carries its canonical ownership/revision checks and preserves workflow state.

`command_run` accepts command, optional queue_id and task_scope. Use returned scope choices (`none`, `all`, `namespace/task_id`). FIFO execution returns a terminal result for fast commands or a cmd_hash for continued reads. Check terminal status and exit_code. Cancellation/recovery remains ownership- and lifecycle-gated.

`command_read` with a known hash reads retained local output across all LogicalAgent without an Access Code or initial attach. With no hash it selects the local all-agent journal. Configured transport authentication still applies.

<!-- contract-example: executor.command_read -->
```json
{"limit":20}
```

<!-- contract-example: executor.command_read -->
```json
{"cmd_hash":"example-command-hash","limit":20}
```

There is no extra command_journal tool: the hashless variant preserves the ten-tool Executor catalog. Continue each collection with the server's opaque next_cursor and the same original query.

## Coordinator operations

`task_get` selects snapshot/detail or one bounded history stream. `task_graph` returns bounded nodes/edges. `task_manage` supports create/update/checkpoint/comment/state/done/review/relate/unrelate/archive under canonical revision and ownership rules; claim/release belongs to Executor. `agent_observe` reads local attachment/lifecycle state. `health` works before attach and distinguishes collection ok from actual healthy/status.

Create requires namespace and isolation_hint. A task_id may be supplied; generated identity makes general create non-idempotent. Create with state=done also requires a non-null result.

<!-- contract-example: coordinator.task_manage -->
```json
{"action":"create","namespace":"example","task_id":"sample","title":"Check the candidate","isolation_hint":"none","state":"ready"}
```

<!-- contract-example: coordinator.task_manage -->
```json
{"action":"state","namespace":"example","task_id":"sample","state":"in_progress"}
```

<!-- contract-example: coordinator.task_manage -->
```json
{"action":"done","namespace":"example","task_id":"sample","result":{"summary":"Checks passed","evidence":"retained-log-reference"}}
```

Archive requires archive_note or note, in addition to namespace/task_id:

<!-- contract-example: coordinator.task_manage -->
```json
{"action":"archive","namespace":"example","task_id":"sample","archive_note":"Superseded by the current task"}
```

Mutation receipts carry transaction-captured state/revision/result. A later writer or failed post-commit readback cannot replace that committed result. Owner-sensitive writes verify ownership in the transaction; reviews and their audit are atomic. Revisions and release reasons remain part of correct concurrency recovery.

## Messaging

Both roles accept recipients/send/read/ack/reply/history. Select a public_name from recipients. Broadcast uses omitted target or broadcast; task addressing uses namespace+task_id. Default scope is fleet; local sends no peer request.

<!-- contract-example: role.message -->
```json
{"action":"recipients","scope":"fleet","limit":20}
```

<!-- contract-example: role.message -->
```json
{"action":"send","target":"broadcast","scope":"local","text":"The candidate is ready for review","mode":"notify"}
```

Local acceptance, recipients/obligations and forwarding jobs commit before the first peer request. Sender, duplicate LogicalAgent and expired local recipients are excluded. A disconnected peer yields queued/partial plus pending_peers and real delivery errors; retain message_hash while durable retry recovers delivery. A bounded forwarding wait also covers a busy worker lock.

Read/ACK/history use local state. Notify is acknowledged only after safe response projection. Required ACK gates command admission; alert requires an actual reply even after ACK. Reply and local obligation release commit together. Parent-thread routing returns the reply to the original execution node. History/keyset cursors survive consumed inbox rows and retention. Full payload/proof byte budgets are checked before acceptance; oversized full read can be retried as bounded summary without silently acknowledging the message.

## Operator/mobile HTTP

Operator-authenticated GET routes: `/actions/access/slots`, `/actions/access/slots/{slot_id}`, `/actions/access/defaults`. POST `/actions/access/mutate` has create/defaults/policy/deadline/end/suspend/resume/rotate/delete. Its explicit idempotency_key is an operator API field, not a role-tool argument. Existing-slot changes require slot_id and expected_revision; defaults requires expected_revision. All examples require current operator authorization.

<!-- contract-example: operator.mutate -->
```json
{"action":"create","mode":"persistent","idempotency_key":"example-provision-slot","policy":{"duration_seconds":1200,"cooldown_seconds":60,"rearm_enabled":true}}
```

<!-- contract-example: operator.mutate -->
```json
{"action":"policy","slot_id":"example-slot","expected_revision":1,"idempotency_key":"example-policy-change","policy":{"release_on_end":true}}
```

Kind is set at creation. Defaults affect subsequent issuance; per-slot policy controls existing slots. Deadline changes require an active cycle and a timezone-aware deadline later than that cycle's start. Issue/rotate can disclose the code in their sensitive receipt; list/default views contain safe metadata. The operator surface exposes no shell tools.

## Validation, errors and repeats

Inputs use permissive planning schemas; strict runtime types, bounds and action conditions remain authoritative. Planning annotations preserve all create/archive constraints. The effective catalog/schema/effect baseline is access_mesh_schema_baselines_v2.json. Most planning inputs are below 8 KiB; full task_manage has a 40 KiB ceiling, outputs below 2 KiB.

Handled application errors use MCP isError:false, ok:false and a structured error object. Read code/details/outcome/retry/reason/path; JSON text and structuredContent agree. Reconcile unknown outcomes before side-effecting retry. A committed-but-queued message is not a failed local send.

Automatic task replay keys include normalized payload and trusted caller/work-session/epoch/request ID, including repeated nonzero and zero IDs. Exact retries return their durable result after gate checks. A constant command request ID alone does not ensure exactly-once shell execution; inspect cmd_hash/journal before repeating commands after uncertainty.

Read/mutate/destructive/idempotent annotations and OpenAPI x-openai-isConsequential describe real operation effects, independently of authorization. Mixed tools use aggregate effects plus action-specific metadata. Role attach is idempotent/non-destructive; command run/recovery is open-world/non-idempotent; generated task creation is non-idempotent.

## Legacy compatibility

Endpoint: `/mcp`.

Legacy compatibility catalog: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

Existing legacy clients keep their session start/end/interrupt and Access Code compatibility path. Those actions are separate from V2 role attach. Native primary acceptance uses Access1/Executor10/Coordinator8 on both FirstByte and BacLOUD; legacy-only tests do not establish it.
