# Architecture C: local privileged executor

The application continues to own durable command identity, queue/FIFO admission,
PID/input fences, output persistence and terminal-state compare-and-set. The
`UnixExecutionAdapter` implements the unchanged ExecutionPort v1 without launching
shells or performing native process signalling in the network-facing process.
Architecture D selects the adapter, installs service units, changes API identity,
and validates live cutover/rollback. Merely merging C does not switch production.

## Local contract and authority

Run `python -m terminal_mcp.terminal.executor_service --socket
/run/terminal-mcp/executor.sock --allowed-uid API_UID --shell /bin/bash --cwd /
--user root --grace 1.0` as the separately managed executor. The socket directory
must exist, be absolute without symlink ancestors, and not be group/world writable.
Its group should allow the API to traverse it. The executor creates a 0660 socket;
its effective group must allow the API to connect. A live socket, foreign-owned
socket, symlink or regular file is never unlinked. A stale owned socket is reclaimed
only after a refused connection and inode revalidation. Use systemd
`KillMode=control-group` so an executor crash also kills descendants before restart.

The only listener is AF_UNIX. Linux SO_PEERCRED enforces an explicit UID allowlist.
The executor has no public MCP, HTTP, OAuth, Console or Fleet listener, imports no
repository, and neither claims durable queues nor writes authoritative command data.
JSON frames are versioned, limited to 1 MiB and checked against operation-specific
fields. Read payloads are at most 65536 bytes before base64 encoding. The service
limits concurrent connections, replay responses and active executions. Default
spool capacity is 16 MiB per execution and 16 executions; overflow is explicitly
reported as truncated, while the producer pipe continues to drain.

A random API-client generation fences older clients, including attempts by a
previous client to reclaim its generation. Opaque handles, not diagnostic PIDs,
authorize process operations. Repeated spawn/input requests are idempotent; conflicting
input fails closed. Request retries join the same cached operation. Byte-offset reads
are non-destructive, so loss of a response does not consume output. A new executor
boot negotiates a different generation: an uncertain old operation is never replayed
across that boot. A diagnostic capture has a bounded service-owned timeout; API
cancellation explicitly cancels and joins that operation, rather than abandoning it.

## API restart and output recovery

A reconnectable adapter advertises optional `recover`, `input_written` and
`output_truncated` capabilities; the core ExecutionPort remains unchanged.
The scheduler detaches from remote processes on API shutdown instead of killing
them. At startup/reconciliation it looks up each durable running command by identity.
A matching existing execution is reattached, not spawned again. Already-admitted
stdin is not reissued. A never-admitted shell must pass the durable PID/input fence
before receiving input. Queue authority remains held throughout reattachment.

Output replay restarts at byte zero of the executor spool. The application parses
logical lines and commits them with a per-command logical-line offset. Offset
validation, duplicate skipping and insertion share one OutputStore transaction.
Thus a crash either before or after a commit does not duplicate committed lines or
lose uncommitted lines that remain in the spool. No authoritative schema migration
is required. The existing output byte limits, explicit truncation and retention
policy remain authoritative; the spool is not a second command-state database.

If the executor is lost, a durable running command is never submitted again. Once
process loss is established it becomes failed, with possible output loss marked
explicitly. An unowned still-live PID or an unavailable executor retains queue
exclusion rather than allowing potentially overlapping side effects. Executor
restart can be detected without restarting the API. Completed output is persisted
before the remote handle is released. Full executor loss cannot preserve the
uncommitted disposable spool; it is reported, not silently presented as complete.

## Verification and deployment gate

`tests/test_execution_ipc.py` covers real Unix transport, UID rejection, strict
framing, ownership and generation fencing, spawn/stdin replay, offset read replay,
stale-socket recovery, bounded/truncated spool, capture timeout/cancellation,
API restart with exactly-once output and FIFO exclusion, executor loss without
side-effect replay, and executor restart without an API restart. The existing
ExecutionPort, queue, output and finalization suites remain required regressions.

Architecture D must validate the exact candidate on approved test nodes, including
nonroot API/root executor permissions, unit cgroup cleanup, installation, recovery
ordering and rollback. The Fleet-control database integrity gate remains mandatory
for topology cutover. A source/unit-test pass is not a production-cutover claim.
