# terminal-mcp

`terminal-mcp` — Python-сервис управляемого доступа к Linux-терминалу через MCP, HTTP Actions и Console.

Текущая версия приложения: **0.13.1**.

## Canonical

- Репозиторий: `https://github.com/U-ruRu/terminal-mcp.git`.
- Development repository: `/workspace/terminal-mcp`; canonical worktree: `/workspace/terminal-mcp-integration`.
- Каноническая ветка разработки: `integration/M3-functional-candidate`.
- Python: `>=3.11`.
- Runtime stack: FastAPI, Uvicorn, MCP SDK, Pydantic, SQLite/aiosqlite.
- Version source: `src/terminal_mcp/version.py`.

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

| Tool | Назначение |
| --- | --- |
| `session` | WorkSession lifecycle: `start`, `end`, `interrupt` |
| `observe` | bounded reads для `sessions`, `tasks`, `namespaces` |
| `message` | inbox, history, send, ACK, reply, alert |
| `task` | managed-task mutations и review |
| `cmd` | `run`, `read`, `cancel`, `recovery` |
| `context` | instance context: `list`, `create`, `update`, `delete` |
| `health` | application, terminal, workflow и diagnostics health |

Managed connector identity формируется на сервере из provider request metadata. `ActorContext` связывает transport principal, provider identity, node identity, `LogicalAgent`, `WorkWindow`, `WorkSession`, endpoint role и contract version.

Persistent Access slot поддерживает compatibility binding. `mode=legacy` создаёт временный Access slot.

Коллекции используют bounded pages и opaque cursors. Current-state task projections: `TaskListItem`, `TaskSnapshot`, `TaskDetail`, `TaskWorkingSet`, `TaskReceipt`, `TaskHistory`.

## Application architecture

```text
MCP / HTTP Actions / Console / Fleet
                ↓
          ActorContext
                ↓
        Application API
                ↓
 SessionGate / Tasks / Messages / Commands / Context / Health
                ↓
 Repositories / Scheduler / Fleet authority / ExecutionPort
                ↓
     in_process | Unix executor
```

Application layer: `src/terminal_mcp/application/`.

Core domain and durable orchestration: `src/terminal_mcp/core/`.

MCP adapter: `src/terminal_mcp/mcp/`.

HTTP/Console adapters: `src/terminal_mcp/http/`.

Execution adapters: `src/terminal_mcp/terminal/`.

Storage adapters: `src/terminal_mcp/storage/`.

## Execution topology

Supported execution modes:

- `in_process` — shell execution inside the API process.
- `unix` — privileged executor over `/run/terminal-mcp/executor.sock`.

Split topology:

- API service: `terminal-mcp.service`.
- Executor service: `terminal-mcp-executor.service`.
- Executor socket: `/run/terminal-mcp/executor.sock`.
- Queue authority and command state: API/application service.
- Process spawning: `ExecutionPort` implementation.
- Executor peer authorization: Unix peer credentials and API UID allowlist.

Operational split reference: [`docs/architecture-split-service-cutover.md`](docs/architecture-split-service-cutover.md).

## Durable state

| Data | Path |
| --- | --- |
| Application state | `/var/lib/terminal-mcp/terminal-mcp.sqlite3` |
| Auth state | `/var/lib/terminal-mcp/auth.sqlite3` |
| Fleet control | `/var/lib/terminal-mcp/fleet-control.sqlite3` |
| Output cache | `/var/cache/terminal-mcp/output.sqlite3` |
| Runtime config | `/etc/terminal-mcp/terminal-mcp.env` |
| Releases | `/opt/terminal-mcp` |
| Backups | `/var/backups/terminal-mcp` |

Application DB stores sessions, managed identity, tasks, claims, dependencies, relations, reviews, messages, command metadata and context. Output cache stores bounded terminal output with independent retention.

## Auth

MCP and Actions auth modes: `none`, `bearer`, `oauth`.

OAuth supports Authorization Code, PKCE S256, Dynamic Client Registration, refresh rotation and JWT access tokens.

Metadata endpoints:

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource`

Public schema and health:

- `/openapi.json`
- `/health/live`
- `/admin`
- `/privacy`

## Deployment

Install:

```bash
sudo ./deploy/install.sh install
```

Update:

```bash
sudo ./deploy/install.sh update
```

Health:

```bash
sudo ./deploy/install.sh doctor
```

Split topology preparation:

```bash
python -m terminal_mcp.deployment.split render --env-file /etc/terminal-mcp/terminal-mcp.env --api-user terminal-mcp --api-uid <uid>
python -m terminal_mcp.deployment.split check --env-file /etc/terminal-mcp/terminal-mcp.env
```

Split activation and rollback:

```bash
python -m terminal_mcp.deployment.driver activate --env-file /etc/terminal-mcp/terminal-mcp.env --api-user terminal-mcp --approved-gates
python -m terminal_mcp.deployment.driver rollback --env-file /etc/terminal-mcp/terminal-mcp.env --api-user terminal-mcp --approved-gates
```

## Role contract implementation

Task: `MCP-ROLE-ENDPOINTS-V1-001`.

Executor v1 endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor v1 catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator v1 endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator v1 catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Оба role endpoint используют общий Application API, общий Session Gate, общие task/message/command projections и единое persisted state. Текущий `/mcp` обслуживает compatibility window.

Task: `TMCP-PUBLIC-METADATA-001`.

MCP annotations и OpenAPI `x-openai-isConsequential` получают operation-level классификацию `read-only`, `mutating`, `destructive`, `idempotent` по фактической семантике операций. Contract tests закрепляют эту матрицу.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

Repository privacy check:

```bash
python3 scripts/check_repository_privacy.py
```

## References

- Packaged operations skill: `dist/terminal-operations.skill`.
- Local operator context: `PROJECT_CONTEXT.md` (git-excluded).
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`skills/terminal-operations/references/tool-contract.md`](skills/terminal-operations/references/tool-contract.md)

## Current release scope

Develop and integrate on Secondary in `integration/M3-functional-candidate`. Release acceptance targets FirstByte and BacLOUD. Secondary, Main and Tokyo retain their deployed runtimes. Reuse the canonical worktree and coordinate concurrent changes by file ownership.
