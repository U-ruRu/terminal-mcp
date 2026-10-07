# Split-service deployment

Status: implemented in canonical `0.13.1` architecture.

## Topology

| Component | Value |
| --- | --- |
| API service | `terminal-mcp.service` |
| Executor service | `terminal-mcp-executor.service` |
| Execution mode | `unix` |
| Socket | `/run/terminal-mcp/executor.sock` |
| Runtime directory | `/run/terminal-mcp` |
| Executor config root | `/etc/terminal-mcp-executor` |
| API config | `/etc/terminal-mcp/terminal-mcp.env` |

API/application service owns public transports, Application API, Session Gate, queue authority, command state and durable application stores.

Executor service owns privileged shell process execution through the versioned local IPC contract.

Unix peer credentials and API UID allowlist authorize executor requests.

## Preparation

Render:

```bash
python -m terminal_mcp.deployment.split render \
  --env-file /etc/terminal-mcp/terminal-mcp.env \
  --api-user terminal-mcp \
  --api-uid <uid>
```

Check:

```bash
python -m terminal_mcp.deployment.split check \
  --env-file /etc/terminal-mcp/terminal-mcp.env
```

The check validates runtime paths, service configuration, SQLite readability, disk capacity, executor socket configuration and topology inputs.

## Activation

```bash
python -m terminal_mcp.deployment.driver activate \
  --env-file /etc/terminal-mcp/terminal-mcp.env \
  --api-user terminal-mcp \
  --approved-gates
```

Activation sequence:

1. Acquire local deployment lock.
2. Validate stores and topology.
3. Snapshot topology files and permissions.
4. Install executor unit and API drop-in.
5. Reload systemd.
6. Start and probe executor.
7. Start and probe API.
8. Persist activation journal.

Activated API environment:

```env
TERMINAL_MCP_EXECUTION_MODE="unix"
TERMINAL_MCP_EXECUTOR_SOCKET_PATH="/run/terminal-mcp/executor.sock"
```

## Rollback

```bash
python -m terminal_mcp.deployment.driver rollback \
  --env-file /etc/terminal-mcp/terminal-mcp.env \
  --api-user terminal-mcp \
  --approved-gates
```

Rollback restores the captured topology files, permissions and service configuration. Durable application, auth and fleet databases retain their current state.

## Runtime invariants

- API service remains the authority for queue and command state.
- Executor process lifecycle is independent from API process lifecycle.
- API restart reconnects to persisted command state.
- Executor restart reconciles through the execution contract.
- Split driver uses journaled topology transitions.
- Installer `doctor` remains the service health entry point.
