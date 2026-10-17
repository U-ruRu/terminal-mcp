# Connector runtime contract

The [Mesh product contract](mesh-product-contract.md) is normative. Access, Execution and Coordination have identical agent behavior in Standalone, a one-node Mesh and a multi-node Mesh. Versioned endpoint paths identify the wire contract; topology does not select a different catalog.

## Published surfaces

Access endpoint: `/terminal-mcp/access/v1/mcp`.

Access catalog: `session`, `get_events`.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

There is one generated agent catalog and one strict runtime contract. The former combined MCP endpoint and role session start/end/interrupt behavior have been removed.

## Access and role binding

Only Access `session` accepts `start` and `end`. Public start takes no mode or session number and returns `ok: true` plus a four-digit `session_number`. End returns `ok: true`. The trusted Access connector context binds the operation. The operator provisions and manages slots through HTTP/Console.

In a Mesh, any Access Connector forwards registration and registry mutation to the single primary. Its slots are valid on every current member. Primary failure does not authorize independent follower issuance. Standalone has the same flow against its local registry.

Execution and Coordination accept `session(action="attach", session_number=...)` and `session(action="intent", intent=...)`. Attach needs no public issuer selector. A connector binding is idempotent for the same number and rejects rebinding to another number. Intent updates the local work declaration; it does not create or extend a session. Subsequent domain calls derive their actor and epoch from trusted context and omit the number.

New attach checks the current registration authority. Already admitted local operations use the verified local grant, policy, deadline, epoch and intent gates. Network loss does not extend a deadline or reopen a revoked local grant. Namespace fences reject old requests throughout a transition, including a request delayed until after membership changes.

## Slots and policy

Slot kinds are `temporary` and `persistent`, immutable after creation. Logical identity and public name identify the same registration on members of its Mesh. Local WorkSession identity and execution ownership remain node-local.

Each Mesh has one inherited common policy. A slot can override it or reset to inheritance. A member cannot replace the common policy with node-specific defaults. Standalone uses the same model locally. Join selects the new Mesh's policy and does not import previous slots.

Policy fields are `duration_seconds`, `cooldown_seconds`, `rearm_enabled`, `warning_seconds`, `draining_seconds` and `release_on_end`. Thresholds must be nonnegative and below duration. Ordinary calls and attach do not extend an active deadline. Future-policy changes and explicit active-deadline changes are different operations.

Temporary claims release on end/expiry. Persistent claims follow release-on-end policy. Suspend and delete fence access and initiate exact execution/claim cleanup. Cleanup preserves task state, checkpoint and result. Unfinished cleanup remains a concrete admission blocker.

## Operator API

Authenticated read endpoints are `/actions/access/slots`, `/actions/access/slots/{slot_id}` and `/actions/access/defaults`. `POST /actions/access/mutate` supports create, defaults, policy, deadline, end, suspend, resume, rotate and delete. Requests use explicit idempotency keys and revision guards; receipts bind the original request. List responses exclude secret session numbers.

Mesh common policy writes use `/actions/fleet/control/policy`; autonomous defaults use the native Access defaults mutation. Slot views expose current and inherited policies, override state and applicability to the next session. A reset to inheritance uses the expected parent revision.

Topology changes use the paired application's control API. Primary selection is `POST /actions/fleet/control/primary` with `node_id`, `operation_id` and optional `expected_topology_revision`. The operation identity is checked before CAS on replay. Membership transitions preserve the complete source registry and clear only the departing node's old namespace.

## Validation, errors and replay

Inputs and outputs publish universal object schemas (`{"type":"object"}`). Per-tool descriptions document fields, defaults, action rules, bounds and errors. Strict server validation runs before mutation and is the authoritative contract. There is no setting that restores an older schema or runtime.

Validation and admission errors include structured `code`, `reason`, `path`, `expected` and `outcome: not_committed` as applicable. Handled application errors use MCP `isError: false`, `ok: false`, and an `error` object with `code`, `details`, `outcome` and `retry`. Text and structured results represent the same outcome.

Task replay includes normalized payload, trusted principal/role and local work identity. Different payloads with the same JSON-RPC ID are distinct; exact retry returns its committed receipt. The current gate still applies. Shell launch with a constant ID is not an exactly-once contract. Read the returned `cmd_hash` or local journal before retrying an uncertain command.

## Commands and tasks

`command_read` accepts a known local hash without attach and can read another agent's retained output. With no hash it pages through the local all-agent command journal. Transport authorization applies in both cases. Opaque cursors must be reused unchanged with their query context.

Command run, cancel and recovery retain local ownership, queue and lifecycle checks. A successful launch is not proof of successful execution. Inspect terminal status, exit code and the actual result.

Task claim/release preserves workflow state and saved work. Explicit state operations change state; create sets initial state; property updates and checkpoint writes preserve it. Mutation receipts are captured within the same transaction as the task and ownership change. A later projection failure does not turn a committed mutation into an uncommitted one.

## Messages and diagnostics

Both working roles provide recipients/send/read/ack/reply/history on the node handling the connector call. Local is the supported message scope. Recipients use active local public names; unknown remote-only recipients are not silently queued. A broadcast omits target or selects broadcast.

Notify read acknowledges only rows successfully surfaced within the response budget. Ack messages require acknowledgement; alerts require a reply. ACK does not replace reply. Local reply creation and obligation release commit atomically. History and paging remain bounded and use local storage. Retained messages from an old registration namespace do not become readable through a new namespace.

Coordinator health and allowed command-output reads work before attach. Health collection success is separate from component health. Diagnostics omit raw provider identifiers, private keys and session numbers.

## Generated contracts and verification

Generated catalogs, strict sidecars, metrics and provenance are produced from the single runtime. Action annotations describe actual effects and never grant authorization. Operator mutations have corresponding OpenAPI consequence metadata.

Atomic checks cover role action rejection, equal discovery, registration through a follower, cross-member attach, namespace isolation, common policy/overrides, membership blocking, source preservation, primary handoff and interrupted recovery. Full source acceptance is tied to an exact clean commit. Installation and live checks require a separately authorized release; see [deployment](access-mesh-deployment.md).
