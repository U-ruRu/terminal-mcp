# Terminal MCP architecture

Current application version: **0.14.4**. Durable runtime schema: **23**. Access Mesh V2 adds its transactional grant/session/receipt tables through its store initialization; the general runtime schema number is not a complete binary rollback compatibility check.

## Public boundary

Access endpoint: `/terminal-mcp/access/v1/mcp`.

Access catalog: `session`.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

FirstByte and BacLOUD each host all three roles: six primary connectors. The Access catalog performs issuer issuance/lifecycle only. Executor and Coordinator use the same application model with distinct capabilities. Their `session` is attach-only in Access Mesh mode.

Legacy compatibility endpoint: `/mcp`.

Legacy compatibility catalog: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

Legacy provider bootstrap/session-start behavior belongs to that compatibility surface. It is not the V2 role-admission path.

## Identity and ownership of authority

```text
trusted ProviderMetadata + authenticated principal + endpoint role
                         ↓ ConnectorBinding
             (issuer_node_id, AccessSlot, LogicalAgent)
                         ↓ replicated grant and policy
       execution node A                  execution node B
       local WorkSession/epoch           local WorkSession/epoch
       local clock/write gate            local clock/write gate
       native commands/tasks/messages    native commands/tasks/messages
```

`ActorContext` carries authenticated transport identity, supported provider metadata, endpoint role and contract version. Provider/principal fields are trusted adapter inputs; tool arguments never choose their own LogicalAgent or session epoch. A four-digit Session Number is resolved in its issuer namespace. Initial attach binds a connector identity to one slot; another slot requires a distinct valid binding context rather than rebinding the existing row.

Each issuer independently creates its own LogicalAgent and immutable slot kind (`legacy` or `persistent`). A stable issuer-qualified `public_name` identifies that LogicalAgent on both nodes and across roles. Working role bindings are distinct, while the underlying LogicalAgent is shared. There is no fixed single-role WorkSession handoff requirement in V2.

The issuer authors signed grant events. An authenticated, explicitly configured peer validates issuer/event provenance and applies an idempotent local replica. Slot revisions, event identity, durable outbox/inbox acknowledgements and catchup handle reordered/duplicate delivery and missed network notifications. Native ownership records protect against identity collisions. Slot suspension/deletion and code rotation retain durable revocation/security history.

## Local session gate

`AccessMeshApplication` resolves an attached grant locally. Writes consult the local policy, slot state, fixed cycle anchor/deadline and cleanup state. Reads use `observed_identity` and do not create WorkSession rows, renew deadlines or mutate epochs. Attach/write admission materializes a local WorkSession only when the replicated policy allows it.

A cycle contains active, optional warning/draining, then expired or cooldown. With rearm enabled, the next cycle follows the replicated anchor, duration and cooldown. Repeated operations do not extend the deadline. The first explicitly changed deadline is honored; subsequent rearm cycles use the slot policy. `SessionUpdated`, `SessionEnded` and per-slot policy controls are issuer events, not calls that each execution connector must relay synchronously.

The ordinary gate performs no issuer RPC per operation. During a partition, a node enforces already-replicated deadlines and policies; it learns newer issuer events when communication/catchup resumes. Local code-free command reads, inbox, history and ACK do not depend on issuer availability. Attach to a previously unseen grant requires that grant to have reached the local replica.

Local expiry/revocation persists cleanup work. Execution is fenced/drained before the relevant claims are released. A failed claim cleanup is isolated and retryable; a late cleanup carries exact claim/session identity and cannot remove a successor claim. Pending execution cleanup retains the admission fence. Read diagnostics expose `cleanup_pending`; new mutable work waits for safe completion.

Legacy ownership releases on end/expiry. Persistent ownership is retained by default and releases on end/expiry when `release_on_end=true`. Suspension and deletion revoke the slot and trigger cleanup independently. Both kinds use the same local deadline/rearm machinery; Mobile/Console is an operator interface, not another kind.

## Application and storage layers

```text
MCP / HTTP Actions / Console / Fleet adapters
                   ↓ ActorContext
              Application API
                   ↓
SessionGate · Tasks · Messages · Commands · Health · Operator
                   ↓
Repositories · Scheduler · Replication · ExecutionPort
                   ↓
SQLite durable state · bounded output cache · shell executor
```

`application/` contains transport-independent use cases. HTTP routers belong in `http/`; SQL and transaction helpers belong in `storage/`. Blocking SQLite work is offloaded from the event loop. Public metadata is attached at the transport adapter; authorization remains an application boundary.

| Store | Responsibility |
| --- | --- |
| `terminal-mcp.sqlite3` | native tasks, claims, messages, command metadata, sessions and context; mesh grant replicas, attachment/activity, cleanup, event/message outbox and replay receipts |
| `auth.sqlite3` | principals, clients and credential/security state |
| `fleet-control.sqlite3` | configured Fleet topology and control |
| `output.sqlite3` | bounded command output with independent retention |

Durable stores keep audit and mutation effects in the appropriate SQLite transaction. Access security/revocation records are not treated as disposable caches. Output pruning does not erase command metadata, tasks or message receipt history.

## Tasks: state, ownership and committed receipts

Task states are `ready`, `in_progress`, `blocked`, `deferred`, `done`. Initial state is assigned by create. Subsequent state changes require `state` or `done`; property `update` rejects a state argument. Claim and release preserve the explicit state. Session expiry, end, suspension and deletion preserve state, checkpoint and result while changing ownership according to slot policy.

Owner-sensitive writes validate the exact claim-id snapshot in their write transaction. Revision checks also guard policy/output changes between preflight and commit. Executor task_comment supports comment (default) and checkpoint without expanding the ten-tool catalog. Safe participant property changes retain their established permissions; comments remain independent append operations. `review` and its audit event commit together. Relations, checkpoint, archive, state and dependency changes follow their ownership and dependency guards.

Every successful mutation family captures the resulting task, ownership, dependency and relevant legacy-session liveness snapshot inside the transaction. The returned `TaskReceipt`/`TaskWorkingSet` reflects that commit, even if another writer later advances the task or a post-commit read fails. Revisions identify the captured record, not an arbitrary newer readback. Canonical projections remain `TaskListItem`, `TaskSnapshot`, `TaskDetail`, `TaskWorkingSet`, `TaskReceipt`, `TaskHistory`.

Automatic task replay keys hash normalized domain fields with trusted caller, role, LogicalAgent, WorkSession, epoch and observed MCP request ID. Reused zero or nonzero IDs with different domain requests produce different mutations. Exact retries return the durable original receipt after the current session gate is checked.

## Commands and ExecutionPort

The API/application service owns admission, numbered FIFO queues, task attribution, command metadata, cancellation/recovery and output pagination. `ExecutionPort` implements process execution: `in_process` or Unix IPC. Split services are `terminal-mcp.service` and `terminal-mcp-executor.service`; socket `/run/terminal-mcp/executor.sock`; Unix peer credentials and an API UID allowlist authorize executor requests.

`command_run` has a bounded inline-completion path; longer execution returns a `cmd_hash` for continuation. Code-free `command_read(cmd_hash=...)` can read retained local output belonging to any LogicalAgent. Omitting `cmd_hash` selects a bounded local command journal across agents, including identity metadata. This does not add a new tool to the Executor catalog or bypass configured transport authentication. Command cancel/recovery retain their write/session/ownership gates.

Command launch replay uses durable reservations where a unique server-observed operation ID is available. A reused constant ID alone cannot distinguish intentional repeated shell execution from a transport retry. After uncertain launch, reconcile persisted command state before another side-effecting launch.

## Native local-first messaging

Local acceptance writes `coordination_messages`, recipients, mesh metadata and required outgoing jobs atomically. Native message obligations are the same rows consulted by the command gate. `scope=local` performs local delivery only; `scope=fleet` commits local delivery first and uses a durable authenticated peer outbox. Global sender identity and exclusion metadata prevent duplicate broadcast delivery to the same LogicalAgent. Active local observations filter expired recipients.

Sending reports actual delivery state: `delivered`, `queued`, `partial` or a structured no-recipient error with its real outcome. Public forwarding waits have a bounded total budget, including waiting for the outbox lock; remaining jobs survive restart. Idempotent delivery/receipt processing recovers lost acknowledgements. Complete Fleet envelopes and acceptance proofs are size-checked before commit, preventing permanently unacknowledgeable local obligations.

Inbox/history/ACK are local. Successful notify-page surfacing marks those messages read; oversized output or failed response preflight leaves them unacknowledged. Keyset pagination uses a durable sequence and remains stable while earlier messages are consumed or removed. Alert requires reply; ACK alone preserves that obligation. Reply acceptance and obligation release are atomic. Reply routing returns to the originating node of the parent message, even when the sender has another attachment elsewhere.

## Operator controls

Authenticated operator reads: `GET /actions/access/slots`, `GET /actions/access/slots/{slot_id}`, `GET /actions/access/defaults`. `POST /actions/access/mutate` supports create, defaults, policy, deadline, end, suspend, resume, rotate and delete. It requires an explicit `idempotency_key`; existing-slot/default updates require `expected_revision`. Mode is chosen only at create. The operator API exposes no shell execution.

Per-slot policy controls duration/cooldown/rearm/warning/draining/release-on-end. Deadline changes update the selected active cycle. Defaults apply to issuance defaults; existing slots retain their snapshots until explicitly updated. Slot lists show safe identity/policy/lifecycle metadata; issue/rotate receipts handle the sensitive code.

## Planning, outputs and effect metadata

Inputs use **permissive planning schemas**, with strict runtime validation before mutation. Task planning exposes all action fields, including create/archive conditional requirements, while keeping exact formal constraints in annotations. Output planning is flat/coarse; strict success/error models remain server-side. Handled failures use `isError:false`, `ok:false`, structured `error` and an equivalent JSON text fallback.

`access_mesh_schema_baselines_v2.json` freezes effective catalogs, schema hashes/byte counts and tool annotations. Most role planning inputs are below 8 KiB; the complete task_manage action schema has an explicit 40 KiB ceiling. Output planning is below 2 KiB. Action metadata describes actual read/mutate/destructive/idempotent effects: attach is idempotent and non-destructive; shell/recovery is open-world and non-idempotent; task create with generated identity is not broadly idempotent. Mixed tools publish their most consequential action plus the action matrix. OpenAPI `x-openai-isConsequential` follows the same operation-level semantics.

## Deployment and historical design records

Use [Access Mesh deployment and acceptance](access-mesh-deployment.md) for the current FirstByte/BacLOUD release. The [split-service guide](architecture-split-service-cutover.md) governs execution topology changes. Topology rollback restores units/configuration; binary/database/security-state rollback requires the installer's compatibility gate and an approved recovery plan.

Older design records under `docs/architecture/` explain implementation history. Current public Access Mesh lifecycle, contracts and release boundaries are defined by this document, [connector runtime contract](connector-runtime-contract.md), the effective schema manifest and runtime tests.
