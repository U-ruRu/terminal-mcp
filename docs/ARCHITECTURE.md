# Terminal MCP architecture

Current application version: **0.13.1**. Durable runtime schema: **21**.

## System boundary

```text
MCP / HTTP Actions / Console / Fleet
                ↓
          Transport adapters
                ↓
            ActorContext
                ↓
          Application API
                ↓
 SessionGate · Tasks · Messages · Commands · Context · Health
                ↓
 Repositories · Scheduler · Fleet authority · ExecutionPort
                ↓
       SQLite state · output cache · shell executor
```

## Public MCP

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Role v1 uses server-resolved identity and the shared Application API. Inputs are permissive planning schemas; authoritative runtime validation precedes every operation. Output planning is a flat object with named success fields, `ok` and `error`; strict wire models remain server-side.

Handled application errors use MCP `isError: false` with `structuredContent.ok: false` and a machine-readable `error` object. Bounded collections use opaque `next_cursor` values.

Bootstrap-created managed sessions and temporary legacy sessions release task claims on end, interrupt and expiry. Explicitly provisioned persistent slots retain their separate ownership policy. Checkpoints and task history survive session cleanup.

### Legacy compatibility

Endpoint: `/mcp`.

Legacy compatibility catalog: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

Public inputs use bounded Pydantic schemas. Public collections use bounded pages and opaque cursors. Public outputs use compact canonical projections from application/core modules.

## ActorContext and identity

`ActorContext` carries:

- authenticated principal;
- provider request identity;
- node/server identity;
- `LogicalAgent`;
- `WorkWindow`;
- `WorkSession` and `session_epoch`;
- endpoint role;
- contract version.

Provider-bound ChatGPT sessions derive identity from server request metadata. Managed identity maps provider bindings to fleet-authoritative logical agents. `SessionGate` resolves active work-session state for agent-bound operations.

For first contact on another execution node, the trusted Access registry identifies the home authority. The authenticated home supplies its route epoch and migration state; the peer caches that verified route. Managed lifecycle admission is performed at home, while commands and their replay receipts remain on the execution node.

Temporary claims are leased to the exact `(LogicalAgent, WorkSession, session_epoch)`. Session drainage fences execution before releasing claims. Automatic `ready` → `in_progress` claim transitions are reversed when the last leased owner leaves; explicit state assignments and durable ownership retain their independent workflow semantics. Remote claim-only leases recover after missed revocation, deadline expiry or process restart.

## Application API

`src/terminal_mcp/application/` is the transport-independent application boundary.

Capabilities:

- sessions and managed identity;
- observations;
- tasks and task graph mutations;
- messaging;
- command orchestration;
- instance context;
- health;
- fleet authority and replication;
- operator controls.

MCP, HTTP, Console and Fleet adapters call the same application semantics.

## Managed work model

Identity hierarchy:

```text
Provider identity → LogicalAgent → WorkWindow → WorkSession
```

Task state:

```text
ready → in_progress → done
            ↓
         blocked

deferred → ready
```

Task ownership is derived from live claims. Cooperative tasks support one owner and additional participants. Dependencies, relations, comments, checkpoints, reviews and output states are durable.

Canonical task projections:

- `TaskListItem` — collection item;
- `TaskSnapshot` — compact current state;
- `TaskDetail` — bounded materialized state;
- `TaskWorkingSet` — executor-oriented claimed context;
- `TaskReceipt` — mutation receipt;
- `TaskHistory` — paginated history.

## Messaging

Messages and receipts are durable application state.

Recipient lifecycle:

```text
delivered → seen → read → replied
```

Messaging supports direct recipients, task recipients, inbox/history reads, acknowledgements, replies and alerts.

## Command orchestration

Application service owns:

- command admission;
- numbered FIFO queue selection;
- queue authority;
- durable command state;
- task attribution;
- bounded output projection;
- cancellation and recovery orchestration.

`command_run` semantics support a bounded inline-completion path and queued/running continuation through read operations.

## ExecutionPort

`ExecutionPort` separates application command semantics from process execution.

Implementations:

- in-process executor;
- Unix-socket executor.

Unix topology:

- API unit: `terminal-mcp.service`;
- executor unit: `terminal-mcp-executor.service`;
- socket: `/run/terminal-mcp/executor.sock`;
- executor peer authorization: Unix peer credentials with API UID allowlist.

API restart preserves application command state. Executor restart converges process execution state through the IPC/execution contract.

## Storage boundaries

| Store | Responsibility |
| --- | --- |
| `terminal-mcp.sqlite3` | application state, tasks, sessions, messages, command metadata, context |
| `auth.sqlite3` | auth principals, clients, grants, Access security state |
| `fleet-control.sqlite3` | fleet authority and topology control |
| `output.sqlite3` | bounded disposable command output |

Durable stores use SQLite transactions for state plus audit evidence. Output retention operates independently from durable application state.

## Deployment model

Installer: `deploy/install.sh`.

Split render/check: `terminal_mcp.deployment.split`.

Split activation/rollback: `terminal_mcp.deployment.driver`.

Activation selects `TERMINAL_MCP_EXECUTION_MODE=unix` and the canonical executor socket. Rollback restores service topology/configuration while durable application databases keep their current state.

## Role contract implementation

Task: `MCP-ROLE-ENDPOINTS-V1-001`.

### Executor v1

Endpoint: `/terminal-mcp/executor/v1/mcp`.

Catalog:

1. `session`
2. `task_list`
3. `command_run`
4. `command_read`
5. `command_cancel`
6. `command_recovery`
7. `task_claim`
8. `task_state`
9. `task_comment`
10. `message`

### Coordinator v1

Endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Catalog:

1. `session`
2. `task_get`
3. `task_list`
4. `task_manage`
5. `task_graph`
6. `agent_observe`
7. `message`
8. `health`

Role endpoints share `ActorContext`, Session Gate, Application API, task projections, messaging state and persisted storage.

### Operation metadata

Task: `TMCP-PUBLIC-METADATA-001`.

MCP tool annotations and OpenAPI `x-openai-isConsequential` use operation-level side-effect classification. Contract tests map read-only, mutating, destructive and idempotent behavior to the final public catalogs.

## Detailed design records

- [`architecture/application-core.md`](architecture/application-core.md)
- [`architecture/execution-port.md`](architecture/execution-port.md)
- [`architecture/executor-service.md`](architecture/executor-service.md)
- [`architecture/managed-work-windows.md`](architecture/managed-work-windows.md)
- [`architecture/public-input-bounds.md`](architecture/public-input-bounds.md)
- [`architecture-split-service-cutover.md`](architecture-split-service-cutover.md)
