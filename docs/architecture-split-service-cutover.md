# Architecture D: split-service cutover

## Gate and compatibility

Source preparation is opt-in, not live acceptance. Keep the default `in_process`
until Architecture C restart/output-replay/fencing tests and the Fleet-control
P0 integrity mitigation are accepted on the exact combined candidate. Use
approved Firstbyte/BacLOUD first and keep an independent control connection.
Do not deploy over active development sessions on Secondary.

`TERMINAL_MCP_EXECUTION_MODE=in_process|unix` selects the execution port in the
composition root, before opening stores. Unix mode fails closed: missing,
incompatible or unreachable IPC must never trigger a local shell fallback.
All MCP/HTTP/OAuth/Console/Fleet contracts and authoritative data remain with the
API. The executor gets no application database, public listener or credentials.

## Service and filesystem boundaries

The generated root executor has `KillMode=control-group`, a root-owned `0750`
runtime directory, socket group access for the non-root API and an explicit
SO_PEERCRED API UID allowlist. No PartOf/BindsTo relationship couples executor
lifetime to API restarts. The API drop-in clears capabilities and sets
NoNewPrivileges. The final mode file lives in `/etc/terminal-mcp-executor`, NOT
in the API-writable mutable-credentials directory `/etc/terminal-mcp`.

Provision a dedicated API account and only its dedicated database/cache/log and
mutable configuration paths. Do not make immutable releases, systemd units,
executor config or the journal writable by that account. The operator driver
checks permissions as the actual API UID; it never recursively chowns system
directories, creates users or silently rewrites persisted application state.
The executor uses the same installation's `current/bin/python`; the API keeps
its existing native-SQLite launcher. Use the installed native SQLite loader for
preflight/driver commands when the deployment requires that pinned runtime.

## Render and check without activation

```sh
python -m terminal_mcp.deployment.split render \
  --env-file /etc/terminal-mcp/terminal-mcp.env \
  --api-user terminal-mcp --api-uid 991
python -m terminal_mcp.deployment.split check \
  --env-file /etc/terminal-mcp/terminal-mcp.env
```

Use the real allocated UID, not the example 991. The base env remains
`in_process`; the final root-controlled EnvironmentFile selects Unix only while
the drop-in is installed. Checks reject nonterminal commands, missing required
stores, unreadable/corrupt SQLite and insufficient disk. SQLite is opened with
mode=ro/query_only. Existing control stores are checked even off-authority.
No database backup is created: only three small topology files are journaled,
at most 128 KiB each, with a 1 MiB journal ceiling.

## Explicit same-release activation / rollback

After independent acceptance of C and the P0 gate:

```sh
python -m terminal_mcp.deployment.driver activate \
  --env-file /etc/terminal-mcp/terminal-mcp.env \
  --api-user terminal-mcp --approved-gates
```

The transaction locks locally, checks drain/integrity, snapshots exact file bytes
and permissions, stops API admission, checks again, installs files, reloads
systemd, starts and probes the executor AS THE API UID, then starts/probes API.
An idempotent repeat does not restart either service.

```sh
python -m terminal_mcp.deployment.driver rollback \
  --env-file /etc/terminal-mcp/terminal-mcp.env \
  --api-user terminal-mcp --approved-gates
```

Rollback checks before and after stopping admission, stops the executor,
restores exact prior config/permissions and restarts the compatibility API.
It NEVER restores a DB snapshot or rewinds command, Access, OAuth or Fleet state.
If new work appeared or a store became unreadable, it refuses to kill executor
or replay work. It retains the journal/executor and may leave the API stopped:
use the independent control connection to reconcile before retrying.

Interrupted transactions require explicit journal-driven rollback. The full
snapshot and immutable release identity are validated before rollback changes.
This is not a cross-release binary downgrade. The legacy installer refuses
mutations while the split drop-in exists, BEFORE changing ownership/release
links; doctor remains observational. A coordinated two-service binary upgrade
is a separate acceptance requirement; do not bypass the guard.

## Completion evidence still required

Run real session/command/output/cancel/health checks on the exact combined
release. Restart API during a command: prove one execution and output without
loss/duplicate lines. Kill/restart executor: prove deterministic terminal state,
no uncertain RPC replay and no unauthorized PID-based kill. Verify stale socket
restart, fencing, bounds, drained rollback, unchanged credentials/data and the
full agent contract. A liveness HTTP response is only a startup probe, not
completion of runtime acceptance. Do not mark D Done on source tests alone.
