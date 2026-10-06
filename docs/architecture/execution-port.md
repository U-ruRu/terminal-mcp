# Architecture B: application scheduler and ExecutionPort v1

The deployment still has one API/application process. `app.py` explicitly composes
`CommandScheduler` with `InProcessExecutionAdapter`. The old `LinuxTerminalAdapter`
constructor remains a source-compatibility composition, not a second implementation.
A separate privileged executor, IPC, migration and rollback orchestration belong to
Architecture C/D; this change does not claim those scenarios have been deployed.

## Authority and ownership

The application owns command identity, task/session attribution, durable status,
queue selection and FIFO sequence, admission fences, output persistence and
finalization/reconciliation. `CommandSchedulerPort` describes that application
boundary. `ExecutionPort` only owns native process resources. It neither imports a
repository nor claims queues or writes command/output state.

There are four numbered FIFO queues by default. Explicit selection is preserved;
unspecified selection minimizes `(durable queued + running load, queue_id)`.
`claim_next` holds an immediate SQLite transaction and refuses a new claim while
that queue has a durable running record. This is important after restart or an
uncertain executor response: a live but unowned process must not overlap the next
command in its queue. Different queues remain independent.

The scheduling sequence is:

1. Persist the queued command and its authority/session attribution.
2. Atomically claim the FIFO head, retaining current session/permit checks.
3. Spawn an empty shell through a versioned `ExecutionRequest`.
4. Persist its PID through the existing compare-and-set admission fence.
5. Only after that fence succeeds, send the command to the owned handle's stdin.
6. Read bounded byte chunks and persist output in the application; finish through
   the existing durable compare-and-set/finalization retry path.

A cancellation between claim and spawn therefore cannot resurrect execution: the
PID fence fails before any command input is written. Queued cancellation never
reaches the process port. A surviving unowned PID is observed, never signalled as
though it were locally owned.

## Versioned process contract

`core/execution.py` contains immutable request, opaque handle and status records.
A handle carries an execution ID, ownership token, diagnostic PID and version.
A raw PID alone grants no right to signal, read or write a process. Unsupported
versions are rejected. Core records contain no subprocess/stream/transport types.

The port covers start, spawn, stdin admission, bounded output reads, status/wait,
terminate/kill, release, process-existence observation, capture, health and close.
Output reads accept only integers in `1..65536`. Capture remains the existing
non-durable diagnostics path and does not acquire numbered-queue authority.

Concurrent identical active `spawn` requests return the same handle. Identical
stdin replay is a no-op; conflicting input is rejected. An interrupted/uncertain
stdin write is never sent a second time. Released execution IDs are not an
unbounded replay ledger: durable replay decisions remain in the application.

The in-process adapter retains at most 256 completed handle statuses to let an
in-flight cancel/wait finish after normal worker cleanup. This cache does not
retain native process resources or authorize signalling released handles.

## Completion, loss and cleanup

Root exit is observed independently from inherited output file descriptors, so
background descendants cannot indefinitely occupy a queue. The application drains
output for the existing bounded grace and records truncation as before. Owned
process groups are terminated on cancellation and shutdown. Cancelled diagnostic
capture joins its communication task and terminates its child.

A confirmed lost process becomes failed without replay. If the port cannot prove
that an execution stopped, the application retains durable `running` authority;
that queue remains blocked rather than admitting a potentially overlapping side
effect. Local waiters are released even when process-resource cleanup fails.
Reconciliation can finish a process only after its loss is established.

`CommandExecutionSnapshot` exposes the current canonical command state, queue and
execution-started information for later bounded orchestration. Queue position is
an advisory read and can change concurrently. Architecture B adds no public MCP
arguments, changes no output schema and does not implement the later five-second
`command_run` fast path.

## Verification and deployment

`tests/test_execution_port.py` runs the same application scheduler against a
process-free memory port: four-queue FIFO/default selection, queued and pre-input
cancellation, recovery timeout, startup/mid-execution process loss, durable claim
exclusion and uncertain executor responses. Native port tests cover replay,
ownership, bounded reads and capture cancellation. Structural tests prohibit OS
operations in the scheduler and durable queue/output operations in the adapter.
Existing queue, finalization, persistent-fencing and transport contracts remain
regressions. Packaging, runtime and canary checks must identify the exact commit.
No SQLite schema migration, systemd topology or production cutover is part of B.
