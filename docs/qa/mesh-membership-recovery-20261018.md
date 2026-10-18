# Mesh membership recovery — 2026-10-18

Base: `4dbffe944c48dfd46b92a41c264051886dd9db4f` on
`integration/M3-functional-candidate`.

## Failure and owning boundary

An authority can commit a member join while its Access registry remains fenced
until the participant acknowledges the new topology. The control plane attempted
to apply local Access policy before delivering that committed topology. Policy
inheritance correctly rejected the write with `mesh_transition_pending`, but this
prevented delivery and repeated during periodic reconciliation and startup.

`ManagedFleetControl._apply_local` now defers this specific policy failure for the
coordinator and recovery paths. It keeps the durable Access fence and does not
acknowledge local policy or trust revisions. `replicate` delivers the committed
topology and then applies the latest durable snapshot after the normal completion
path releases the fence. Incoming `apply_replica` remains strict: a participant
whose policy is still fenced must not return a successful acknowledgement.

This changes control-plane ordering. It does not add agent methods, compatibility
modes, schema migrations, or a separate Standalone/Mesh session implementation.

## Console behavior

- Membership is observed from the participant's own control response. A desired
  authority row cannot replace a participant's fresh Standalone state.
- `mesh_transition_pending` means the operation is applying. Fresh GET observations
  reconcile the result without automatically repeating the mutation POST.
- Mesh completion requires applied revisions and ready native trust. Standalone
  completion requires a fresh autonomous native state with `ready=true`.
- A fresh confirmed result clears the associated pending operation error.
- Invalid response contracts retain a safe error code and JSON path. HTTP 200
  business errors retain their public operation code.

## Atomic verification

The existing topology and primary-handoff modules own the backend checks. No new
test module or runner was added. The existing `RegistryProbe` now actually rejects
policy application while its registry guard is set, matching its production port.

New uncovered cases:

1. Committed topology must be delivered before successful local policy application.
   One invariant is parameterized over join, periodic recovery and direct reconcile.
2. Incoming replica application must not acknowledge a still-fenced policy.

The first case and the existing startup/handoff check produced four expected
failures on the unchanged base. The fixed candidate passed all eight selected
backend cases in 0.93 seconds; Ruff and `git diff --check` passed.

```sh
python -m pytest -q \
  tests/test_mesh_topology_transitions.py::test_committed_membership_is_delivered_before_guarded_policy \
  tests/test_mesh_topology_transitions.py::test_replica_does_not_acknowledge_a_fenced_policy \
  tests/test_mesh_topology_transitions.py::test_pending_local_transition_blocks_membership_before_topology_commit \
  tests/test_mesh_topology_transitions.py::test_recovery_finishes_the_committed_local_scope_once \
  tests/test_mesh_topology_transitions.py::test_replica_detach_finishes_locally_without_source_acknowledgement \
  tests/test_mesh_primary_handoff.py::test_startup_keeps_primary_guard_until_unavailable_target_activates
```

Console checks reuse the existing API, observation, readiness and route owners.
Coverage was added or extended for business-error classification, contract paths,
pending recovery, native readiness for Standalone, and participant-based grouping.
The selected 31 checks passed in 6.07 seconds; TypeScript and the production web
build passed.

```sh
cd apps/console
npm test -- \
  src/api/client.test.ts \
  src/connections/controlObservation.test.ts \
  src/connections/membershipReadiness.test.ts \
  src/connections/controlState.test.ts \
  src/routes/Connections.lifecycle.test.ts \
  src/routes/Connections.readIsolation.test.tsx \
  src/routes/Overview.test.tsx \
  src/api/nativeTrustContract.test.ts
npm run build
```

No current test obligation was retired. No comprehensive, stress, or full release
acceptance suite was run. These component results do not claim live phone UI
acceptance; deployed topology and signed APK publication are separate operator
verification steps.

## Backend follow-up: canonical session activation

A subsequent attach failure exposed a mismatch between the number-start
announcement and its committed `SessionStarted` event. The announcement could
sample `active_from` earlier than the canonical event. Replaying the same start
with the canonical timestamp then changed the existing announcement's activation
field, so `MeshSessionNumbers.record_start` raised `idempotency_conflict` and
blocked replica attach/reconciliation.

The follow-up in `application/access_mesh.py` publishes `active_from` from the
committed event, using its effective start only when the event has no separate
activation timestamp. Existing derived announcements can be repaired by
`storage/access_mesh_numbers.py` within the normal registry transaction and
readiness/writer guards. Repair is restricted to an `active_from`-only mismatch
submitted by the current registry authority. The candidate must match accepted
canonical data for the same issuer, slot, event ID and revision: either its
`SessionStarted` journal entry or the slot/session snapshot at that exact revision.
The activation timestamp, start and deadline must all match that proof. A later
session window cannot serve as proof for an earlier event.

Only the derived announcement's activation field is updated. Other changed fields
and unproven activation changes still raise `idempotency_conflict`; accepted
session events, session identity and deadlines are preserved. This repair adds no
schema migration, compatibility mode or agent method.

### Follow-up verification

The existing `tests/test_access_mesh_session_numbers.py` owner contains two
parameterized repair cases, for event-journal and exact-snapshot proof, and the
existing rejection case for an unproven activation change. On unchanged `85242c7`
source with the new tests, the two repair cases failed with `idempotency_conflict`
and the rejection case passed: **2 failed, 1 passed in 0.16 seconds**. With the
backend patch, **all 3 passed in 0.16 seconds**.

The separate atomic application case in `tests/test_access_session_start.py`
checks the announced activation against the committed event when the clock
advances. It failed on unchanged `85242c7` application code: **1 failed in 0.50
seconds**, with the announcement using the earlier sample instead of the event's
activation one microsecond later. The complete narrow follow-up run passed
**all 4 cases in 0.53 seconds**. Ruff passed on all four changed Python files, and
`git diff --check` passed.

```sh
python -m pytest -q \
  'tests/test_access_mesh_session_numbers.py::test_canonical_start_repairs_an_early_announcement[event]' \
  'tests/test_access_mesh_session_numbers.py::test_canonical_start_repairs_an_early_announcement[snapshot]' \
  tests/test_access_mesh_session_numbers.py::test_start_replay_is_exact_and_cannot_shift_activation_timestamp \
  tests/test_access_session_start.py::test_start_announcement_uses_authored_event_time_when_clock_advances
```

### Operator verification

Live timestamp comparison reproduced the mismatch in a session start and a later
rearm: the canonical journal and accepted snapshot agreed, while a replica's
number-start announcement retained an earlier activation time. After installing
the follow-up, perform a normal attach to the same session number and verify
command acceptance. The component results above do not replace this operator
check.

### Published UI artifact

APK `0.2.44` (`versionCode=46`) was built from `85242c7`, signed with the production
key, published and verified. It already contains the Console membership-recovery
changes described above. The session-activation follow-up changes backend code
and its atomic tests only; APK 46 remains the UI artifact for this repair and does
not require a rebuild.
