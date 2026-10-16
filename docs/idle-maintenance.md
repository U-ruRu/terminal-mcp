# Idle lifecycle maintenance

The one-second maintenance bound is unchanged. Request authorization always reads
current authoritative state; background change detection never supplies cached
identity or permissions to an application or MCP request.

## Persistent sessions

`PersistentAgentStore.due_session_slots` starts from the current-session state
index and selects local, non-deleted agents with an expired or stopping session,
excluding current managed work windows. Existing session/agent/window indexes are
used. SQLite normalizes timestamp representations for the eligibility filter; the
coordinator re-reads the session under its operation guard and checks its precise
UTC deadline before fencing. Mesh-managed aliases are excluded only for these
candidates. Historical agents no longer cause per-slot Mesh, window and session
connections every pass. Durable rearm handling is unchanged.

## Access Mesh cycles

`AccessMeshStore.lifecycle_signature` reads the inputs to local cycle maintenance
in a single read transaction. It watches attached identities, their possible
number-group winners, explicit starts/ends, local materializations and pending
cleanup. Unrelated command/output history and activity timestamps do not invalidate
it. The signature is process-local and does not change the persisted schema.

`AccessMeshApplication._maintenance_tick` compares that signature every second.
A changed signature, an expiry/rearm deadline, a failed scan or clock reversal
causes reconciliation. A completed unchanged cycle needs no repeated identity
materialization. Cleanup keeps its independent bounded retry, because command
completion can unblock fencing without an Access mutation. Warning/draining
phases are still calculated from the current clock on the request path; background
side effects remain tied to expiry and rearm.

A paginated sweep retains the signature captured **before its first page**.
Changes to earlier pages cannot be acknowledged by visiting a later page. The
next sweep revisits any mutation that occurred during the scan. Startup forces a
fresh pass; restart requires neither a cache migration nor reconstructed cache data.

## Regression ownership

`tests/test_idle_maintenance.py` owns eligibility filtering, unchanged-work
suppression, required wake conditions, cleanup retry, paginated invalidation and
relevant-versus-unrelated storage changes. Existing native lifecycle, shared restart,
deadline-extension and MCP runtime tests own their public behavior. Full collection
still includes every test module and every parametrized case.
