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
