# Managed WorkWindow core (M6 implementation slice)

## Current activation boundary

This slice adds the durable domain, SQLite schema 20, and the transport-independent
`ManagedSessionApplication`. It does **not** switch any existing HTTP/MCP endpoint
to managed admission. The parent `M6-AGENT-IDENTITY-SESSION-001` remains incomplete
until provider authorization, authoritative Fleet routing, operator UI, bounded
reconciliation composition and explicit cutover are implemented and reviewed.

The Application capability requires an independent `ManagedSessionAuthorizer` and
an actual `ManagedExecutionFence`. There is no default grant or no-op fence.
Provider metadata is identity evidence, not proof of permission. A configured
inbound adapter must resolve the identity; the authorizer must separately verify
an actor's right to use that logical slot. Raw provider values and credentials do
not appear in the managed receipt. Supplying identity metadata cannot bypass the
verified admission, scope, independent grant, principal, role/version or authority
checks.

## One canonical identity/session universe

Existing `logical_agents`, `logical_agent_work_sessions`, command attribution,
task claims and audit records remain canonical. Additive tables hold the provider
binding digest, per-slot policy, WorkWindow snapshots and immutable session
role/version provenance. `ActorContext` can carry a resolved LogicalAgent without
an active WorkSession. Once a session is present, agent/session/epoch must still
be complete. There are no provider SDK dependencies in these domain values.

A current window is unique per logical agent. Starting uses a SQLite
`BEGIN IMMEDIATE` transaction to select/create the window, check the existing
session, allocate the next epoch and create the session binding atomically.
Concurrent identical Starts return one session. Changing role/version on that
active session is rejected. End/Start creates a new session/epoch but keeps the
same WorkWindow and hard deadline. Claims remain owned by the LogicalAgent.

## Policy, CAS and time

A per-slot policy defaults to duration 1380 seconds, warning 180 seconds before
expiry, draining 90 seconds before expiry and rearm 180 seconds after expiry.
Policy updates use CAS and affect future windows only. Each window retains its
captured initial values. Explicit current-window updates accept either total
duration or signed delta and use a separate window revision.

Phases are derived from the current deadline on every operation: extending a
window can move draining back to active. An operator shortening the deadline
into the past expires the window at the time of that change. Delayed hard-expiry
reconciliation preserves the original deadline as expiry time, so restart does
not buy another cooldown period. Expired windows cannot be extended/resurrected.

Draining permits the explicit finalization/read allowlist, including command
read/cancel, task checkpoint/done/release and message ack/reply. It rejects new
independent work with `session_draining` and `return_to_chat`. Starting a session
inside an unexpired draining window does not enable new work: the same shared
operation gate still applies. Expiry rejects work with `session_expired`.

## Revocation and execution

Deadline changes update both the WorkWindow snapshot and canonical active session
deadline in one transaction. Expiry changes the session/slot to `stopping` in that
transaction, before process cancellation. Existing execution admission already
checks that exact canonical session state and deadline.

Only after the injected local/Fleet fence drains execution and the durable
command query reports no queued/running work can finalization complete. Fence
timeouts/failures leave the durable revoked state intact. A committed operator
mutation reports `cleanup_pending`, not an apparent uncommitted change to retry.
Cancellation propagates and never restores authority. Cooldown is persisted only
after session and command drainage, and successor admission checks it atomically.

## Migration and legacy compatibility

The schema installer is additive. Merely starting schema-20 code does not migrate
an existing legacy session into managed mode. Explicit `adopt_legacy_session`
retains session IDs, epochs, original session timestamps and exact hard deadline.
When prior sessions with the same deadline exist, the original window start is
recovered from that chain. Fractional legacy remaining budgets preserve the exact
deadline instead of rounding it into additional time. The old rearm timer is
cancelled without deleting its historical row.

Once a managed window exists, legacy Start/stop/finalize paths reject it with
`managed_session_required`. Legacy expiry/rearm reconciliation skips it. This
prevents an older endpoint from resetting managed timing or contract provenance.
Read-only legacy views still inspect the same canonical rows.

A schema-forward guard continues to reject unknown newer database versions.
Reverting a binary below schema 20 therefore requires the supported snapshot
restore procedure, not editing `PRAGMA user_version`. This slice has not performed
any deployed database migration or production runtime switch.

## Bounded recovery adapter

`ManagedWindowRecovery.tick()` leases only a configured small due batch (default
8, maximum 32), with at most four concurrent recovery calls and bounded item
waits. The batch execution plus cleanup budget must fit its durable 30-second
lease. An interrupted worker leaves the lease to expire; a replacement worker
cannot have its lease released by a late completion from the original worker.
Each completion re-reads the current window, so a stale timer cannot overwrite
an operator extension. Failed execution fences retry with bounded exponential
backoff instead of repeatedly blocking the oldest page of windows. Normal End
cleanup is retried without expiring/replacing the window. Delayed hard-expiry
still retains the original expiry/rearm timestamps.

The recovery schedule is attached transactionally to window changes and session
revocation. It is stored in the same authoritative SQLite snapshot, while the
queue remains a scheduling projection rather than the source of duration policy.
Startup additively reconstructs missing schedule entries without resetting
existing leases. No timer tasks start merely from importing the module. Runtime
composition must explicitly connect its tick to the accepted host lifecycle
before enabling managed endpoints.

Live operation admission now reads slot state, authority epoch, auth generation,
session/window link and exact deadline from one SQLite snapshot. Historical
status/end uses an explicit non-live view; that view does not authorize work.

## Provider resolution, authorization and Fleet routing

`ManagedProviderResolver` converts trusted server-side provider metadata through the
explicit provider registry and resolves only an existing provider binding. It never
creates or chooses a slot. Provider binding lookup is fleet-readable so a non-authority
node can learn the `logical_agent_id`; creating or changing a binding remains restricted
to the canonical slot authority.

`ManagedAuthorityRouter` then compares the canonical slot authority node/epoch with the
Fleet control route. A configured Fleet route must exist, be active, and match both
values exactly. Standalone resolution accepts only a slot owned by the repository's
local authority. Stale or incomplete routing fails closed before session admission.

`ManagedGrantAuthorizer` separately checks the verified principal/credential against
active AuthFoundation grants. The grant must match the concrete client credential, the
required read/execute scope, and either the slot resource or its current authority-node
resource. Operator operations additionally require the `operator` role. The public name
comes from the active Access Authority slot and its authority must match the same route.
These adapters are production-capable boundaries but are not yet connected to public MCP
transport; transport metadata capture, runtime composition and cutover remain explicit
follow-up work.
