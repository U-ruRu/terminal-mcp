# Terminal MCP tool contract

Version: **0.13.1**.

Endpoint: `/mcp`.

Канонический MCP-каталог: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

## `session`

Actions: `start`, `end`, `interrupt`.

Managed provider identity приходит из server request context. Persistent Access code поддерживает compatibility binding. `mode=legacy` создаёт временный slot.

Session result публикует lifecycle state, public name, WorkSession reference, epoch и hard-expiry metadata.

## `observe`

Subjects: `sessions`, `tasks`, `namespaces`.

Filters:

- `namespace`;
- `task_id`;
- `lane`;
- `state`;
- `operational_status`;
- `tags`;
- `show_done`;
- `show_archived`.

Detail: `summary`, `full`.

Collections: `limit` + opaque `cursor`.

## `message`

Capabilities:

- active inbox read;
- history read;
- direct send;
- broadcast send;
- task-addressed send;
- ACK;
- reply;
- alert.

Modes: `notify`, `ack`, `alert`.

Recipient lifecycle: `delivered → seen → read → replied`.

Reads use bounded pagination and `summary|full` detail.

## `task`

Request actions:

- `create`;
- `claim`;
- `release`;
- `update`;
- `checkpoint`;
- `comment`;
- `relate`;
- `unrelate`;
- `state`;
- `done`;
- `archive`;
- `review`.

Task lanes: `implementation`, `review`, `release`, `integration`, `general`.

Task states: `ready`, `in_progress`, `blocked`, `deferred`, `done`.

Priorities: `P0`, `P1`, `P2`, `P3`.

Claim ownership belongs to `LogicalAgent` and survives WorkSession rotation. Claiming claimable ready work transitions it to `in_progress` atomically.

Canonical task outputs use `TaskReceipt`, `TaskSnapshot`, `TaskDetail`, `TaskWorkingSet`, `TaskHistory` and collection projections.

## `cmd`

Request actions:

### `run`

Inputs: `command`, optional `queue_id`, `task_scope`.

Execution uses numbered FIFO queues. A fast completion can return terminal status and the first bounded output page inline. Queued/running execution returns `cmd_hash` for continuation.

### `read`

Inputs: `cmd_hash`, optional session code, `limit`, `cursor`.

Result includes status, bounded lines, terminal outcome fields and next cursor.

### `cancel`

Cancels one queued/running command by `cmd_hash`.

### `recovery`

Executes the recovery command path for operational repair.

## `context`

Actions: `list`, `create`, `update`, `delete`.

Context entry fields: `id`, `summary`, `content`, `primary`.

Summary reads return compact records. Full reads return content.

## `health`

Health publishes:

- application/version;
- storage;
- auth mode;
- terminal user, cwd and privilege;
- scheduler and numbered queues;
- running/finalizing commands;
- worker health;
- output-cache metrics;
- workflow state summary.

## Identity model

```text
provider request identity
        ↓
    ActorContext
        ↓
   LogicalAgent
        ↓
    WorkWindow
        ↓
    WorkSession
```

Application services resolve managed identity and Session Gate state on the server.

## Output model

- current state uses bounded canonical projections;
- collections use opaque cursor pagination;
- mutations use compact receipts;
- command output uses bounded pages;
- public errors use stable machine-readable codes.

## Planned public contracts

### Role v1

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Both role catalogs use the shared Application API, ActorContext, Session Gate, task state machine, messaging state and persisted stores.

### Operation metadata

Task: `TMCP-PUBLIC-METADATA-001`.

MCP annotations and OpenAPI `x-openai-isConsequential` use operation-level classification for read-only, mutating, destructive and idempotent semantics.
