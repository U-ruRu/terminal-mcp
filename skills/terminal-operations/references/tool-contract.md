# Terminal MCP tool contract

Unified Mesh contract. Execution and Coordination use `session(action=attach, session_number=...)` and `session(action=intent, intent=...)`. The examples use synthetic code `0427`; substitute the code issued privately by Access.

Access endpoint: `/terminal-mcp/access/v1/mcp`.

Access catalog: `session`, `get_events`.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Every Standalone node and every member of a one-node or multi-node Mesh exposes these same three connectors. Slot kinds are `temporary` and `persistent`; kind is immutable. The paired application manages Mesh membership and operator slot policy.

## Access and attach examples

Start a session through any Access in the current Mesh and receive only `ok` plus four-digit `session_number`. The primary owns the registration; Standalone issues locally:

<!-- contract-example: access.session -->
```json
{"action":"start"}
```

When a prior session has ended and its cooldown is complete, start a fresh session:

<!-- contract-example: access.session -->
```json
{"action":"start"}
```

End a session through a bound Access connector on any current Mesh member:

<!-- contract-example: access.session -->
```json
{"action":"end"}
```

<!-- contract-example: access.session -->
```json
{"action":"end"}
```

Bind each required Executor/Coordinator once:

<!-- contract-example: role.session -->
```json
{"action":"attach","session_number":"0427"}
```

The same role contract also accepts a session number issued by another Mesh node:

<!-- contract-example: role.session -->
```json
{"action":"attach","session_number":"0427"}
```

The four-digit session_number is sufficient: the Mesh resolves issuer identity. Initial attach is write-once for a connector binding and idempotent for the same session. Rebinding to a different number is a structured conflict. Subsequent role operations omit Session Numbers and client-selected identity, provider metadata and epochs. Access handles end; role tools do not expose detach or local session start/end. Refresh intent before writes using the server deadline; an intent refresh never extends the session deadline.

<!-- contract-example: role.session -->
```json
{"action":"intent","intent":"Review and test the isolated implementation"}
```

## Local lifecycle and policy

A registration retains LogicalAgent/public_name across roles and nodes in its current Mesh. The primary owns the complete registry and common inherited policy; a slot can explicitly override that policy. A local WorkSession/epoch and fixed deadline authorize writes on each execution server. Per-call admission uses local grant/policy/time; issuer RPC is not a read/write prerequisite. Unseen events require network recovery/catchup; known deadlines continue to apply through a partition.

Policy contains duration_seconds, cooldown_seconds, rearm_enabled, warning_seconds, draining_seconds, release_on_end. Local expiry/rearm follows the policy without repeated attach. Warning/draining thresholds are smaller than duration. Pending cleanup keeps a write fence until old execution is drained and applicable claims are released. Temporary slots release at end/expiry; persistent retains ownership unless release_on_end=true. Suspend/delete triggers revocation cleanup.

Claim/release/lifecycle cleanup preserve explicit state, checkpoint and result. States are ready/in_progress/blocked/deferred/done. Create sets initial state; state/done explicitly changes it. Property update preserves state and rejects a state argument. Safe successor claims survive late cleanup through exact claim/session fences.

## Executor operations

`task_list` returns TaskListItem records. `task_claim` accepts claim/release; claim requires claim_intent. `task_state` explicitly transitions workflow and supplies result when completing or blocker_reason when entering an owned blocked state. `task_comment` supports `action=comment` (the default) for appended history and `action=checkpoint` for a durable checkpoint. Checkpoint carries its canonical ownership/revision checks and preserves workflow state.

`command_run` accepts command, optional queue_id and task_scope. Use returned scope choices (`none`, `all`, `namespace/task_id`). FIFO execution returns a terminal result for fast commands or a cmd_hash for continued reads. Check terminal status and exit_code. Cancellation/recovery remains ownership- and lifecycle-gated.

`command_read` with a known hash reads retained local output across all LogicalAgent without a Session Number or initial attach. With no hash it selects the local all-agent journal. Configured transport authentication still applies.

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
{"action":"state","namespace":"example","task_id":"sample","state":"done"}
```

Archive requires archive_note or note, in addition to namespace/task_id:

<!-- contract-example: coordinator.task_manage -->
```json
{"action":"archive","namespace":"example","task_id":"sample","archive_note":"Superseded by the current task"}
```

Mutation receipts carry transaction-captured state/revision/result. A later writer or failed post-commit readback cannot replace that committed result. Owner-sensitive writes verify ownership in the transaction; reviews and their audit are atomic. Revisions and release reasons remain part of correct concurrency recovery.

## Messaging

Both roles accept recipients/send/read/ack/reply/history. Select a public_name from recipients. Broadcast uses omitted target or broadcast; task addressing uses namespace+task_id. Messages, recipients and obligations are local to the current node. Use scope=local; sends do not create peer requests.

<!-- contract-example: role.message -->
```json
{"action":"recipients","scope":"local","limit":20}
```

<!-- contract-example: role.message -->
```json
{"action":"send","target":"broadcast","scope":"local","text":"The candidate is ready for review","mode":"notify"}
```

Local acceptance and recipients/obligations commit atomically. Sender, duplicate LogicalAgent and expired local recipients are excluded. An identity present only in a remote registration replica is not a local recipient. Read the accepted message by its returned message_hash; there is no cross-node forwarding job.

Read/ACK/history use local state. Notify is acknowledged only after safe response projection. Required ACK gates command admission; alert requires an actual reply even after ACK. Reply and local obligation release commit together. Replies remain on the node of their local parent thread. History/keyset cursors survive consumed inbox rows and retention. Full payload/proof byte budgets are checked before acceptance; oversized full read can be retried as bounded summary without silently acknowledging the message.

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

Kind is set at creation. The primary applies one common parent policy to inherited slots. Per-slot overrides remain independent. A member cannot install its own parent override; after Join it uses the destination Mesh policy. Deadline changes require an active cycle and a timezone-aware deadline later than that cycle's start. Issue/rotate can disclose the code in their sensitive receipt; list/default views contain safe metadata. The operator surface exposes no shell tools.

## Validation, errors and repeats

Inputs and outputs publish **object-only agent schemas** ({"type":"object"}); per-tool descriptions document arguments, nullable fields, defaults, action rules, return shapes and public errors. Existing strict Pydantic validators remain authoritative before all mutations. input_validation_failed and input_admission_failed return structuredContent with error.code/reason/path/expected and outcome:not_committed. The precise strict models are in the offline strict-contracts.json sidecar.

Handled application errors use MCP isError:false, ok:false and a structured error object. Read code/details/outcome/retry/reason/path; JSON text and structuredContent agree. Reconcile unknown outcomes before side-effecting retry.

Automatic task replay keys include normalized payload and trusted caller/work-session/epoch/request ID, including repeated nonzero and zero IDs. Exact retries return their durable result after gate checks. A constant command request ID alone does not ensure exactly-once shell execution; inspect cmd_hash/journal before repeating commands after uncertainty.

Read/mutate/destructive/idempotent annotations and OpenAPI x-openai-isConsequential describe real operation effects, independently of authorization. Mixed tools use aggregate effects plus action-specific metadata. Role attach is idempotent/non-destructive; command run/recovery is open-world/non-idempotent; generated task creation is non-idempotent.

## Namespace changes

The paired application manages topology. It can appoint a primary, join, detach
and move nodes without history-based permission checks. Active slots or work in
the affected area block a transition. A durable fence prevents an earlier request
from committing into a namespace after membership changes.

A departing node clears only its local old Mesh registrations, replicas,
bindings and authority. The remaining source Mesh preserves every registry row,
including slots originally issued through that node. A primary departure first
hands the complete registry to the application's successor. The destination
registry and policy replace the departed node's local authority; source slots
are never imported into a different Mesh. Independent node identity and Console
device connections survive.

There is one runtime and one set of role validators. Hot upgrade performs a
one-time conversion of retained data. No configuration flag re-enables old role
session actions, alternate catalogs or the retired /mcp endpoint.
