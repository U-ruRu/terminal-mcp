# Android slots: canonical contract and bounded extension proposal

Base: `7013d3a342396c01a6a0004ec7a01a4c23c71476`, agreed through dot/Sierra-4 at 2026-10-11 22:05 UTC.
Owners: Papa-4 API/models/server contract/integration; Quebec-4 ServerSlots/Slot Details/UI tests/Android build.
No production mutations, code issuance, policy/access/security changes, deployment or lifecycle-notification changes are authorized by this implementation work.

## Existing contract matrix

All endpoints below require the authenticated operator control plane. Managed status and Access Mesh reads require `terminal:read`; mutations require `terminal:execute`. Paired-console OAuth fallback is route-restricted in mixed bearer mode; it must not be widened to unrelated execution/session routes.

| User action | Existing endpoint | Preconditions / request | State change | Confirmation |
| --- | --- | --- | --- | --- |
| View old persistent slots | GET `/actions/console/snapshot`, POST `/actions/persistent/slots/list` | persistent enabled; snapshot can return enabled=false, available=false or error | Read projection only | enabled/available/error + required slots array, never infer empty from missing data |
| View current Access Mesh slots | GET `/actions/access/slots?limit=...&cursor=...`; GET `/actions/access/slots/{slot_id}` | Access Mesh router enabled; local issuer, opaque cursor | Read only | ok + slots + next_cursor; issuer_node_id and slot_id, logical_agent_id are separate identifiers |
| Read / change Mesh policy | GET `/actions/fleet/control`; POST `/actions/fleet/control/policy` | Existing Mesh control authority; duration_seconds, warning_after_seconds, alert_after_seconds, rearm_after_seconds, legacy_admission_enabled, expected_revision | Existing revisioned Fleet policy, existing propagation | Re-read control.policy revision; distinguish committed from convergence |
| Change old Standalone policy | POST `/actions/persistent/policy` | One or more old global policy fields; warning_after < alert_after < duration; no caller revision/idempotency contract | Old controller globals | Re-read snapshot.policy; uncertain response must be reconciled, not blindly retried |
| Read / change Access Mesh issuer defaults | GET `/actions/access/defaults`; POST `/actions/access/mutate` action=defaults | expected_revision, idempotency_key, optional policy and legacy_enabled | Local issuer defaults; existing implementation affects newly issued slots, NOT Mesh policy | Re-read defaults; stable-key replay supported |
| Read managed per-slot policy/window | POST `/actions/persistent/managed/status` | logical_agent_id; correct home authority | Reads existing/lazily initialized managed policy, current WorkWindow and session | policy.revision and window.window_revision are distinct |
| Change managed slot policy | POST `/actions/persistent/managed/policy` | logical_agent_id, expected_revision, default_duration_seconds, warning_before_expiry_seconds, draining_before_expiry_seconds, rearm_after_seconds | Slot defaults copied into NEXT WorkWindow; open window unchanged | Re-read status; revision_conflict requires refresh, no automatic overwrite |
| Change managed current window | POST `/actions/persistent/managed/window` | logical_agent_id, expected_revision; duration_seconds XOR delta_seconds, optional before-expiry thresholds | Current window revision/deadline; shortening may expire immediately; cleanup_pending means committed | Re-read status; previous/current and cleanup_pending, no blind delta retry |
| Create / rotate old access code | POST `/actions/persistent/slots/create`; `/slots/rotate-access-code` | create display_name; rotate logical_agent_id; neither accepts idempotency key in base | Creates slot or rotates secret; code returned once | Re-read metadata, vault only; unknown response must block silent repeat |
| Create / rotate Access Mesh code | POST `/actions/access/mutate` action=create or rotate | create mode; rotate slot_id+expected_revision; both idempotency_key | Durable receipt + slot/event; stable key replay | Re-read slot revision, save returned code privately; no code in list/log/report |
| Change Access Mesh per-slot policy/window | POST `/actions/access/mutate` action=policy or deadline | slot_id, expected_revision, idempotency_key; policy patch or timezone-aware deadline_at | SlotPolicyChanged or SessionUpdated, no invented managed-window identity | Re-read authoritative slot; preserve stable key on uncertain transport |

## Verified distinctions / gaps

1. `app.py` independently gates Access Mesh and old persistent routers. `service._persistent_console_snapshot` reads only the old persistent backend. The Android client currently never loads `/actions/access/slots`. `AccessMeshStore._native_slot` bridges Access slots into logical_agents, so some Access slots can appear in the old snapshot with selector=null and a different access shape. Their effective policy and mutations still belong to Access Mesh; these stores must not be conflated.
2. Old global warning/alert fields are seconds AFTER start. Managed policy and Access Mesh warning/draining fields are seconds BEFORE expiry. `SlotSessionPolicy.from_legacy` defines the conversion.
3. `SlotSessionPolicy` accepts positive durations, nonnegative before-expiry thresholds/rearm, draining <= warning. No client-side 23-minute cap exists in that contract. 10800 seconds is valid. Access Mesh additionally requires each threshold < duration.
4. Base managed policy has no source/reset field. `_policy_on` stores a snapshot. Access Mesh policy is also a concrete slot policy; defaults are local issuer state. Copying current values into a slot must NOT be labeled dynamic inheritance.
5. Code validity is not a generic fixed TTL in these responses. Access code, slot identity and current session deadline are different. Do not display a session deadline as a code expiry without server metadata.
6. Existing slots without explicit source metadata must keep their effective policy. A migration must not silently reclassify them as inherited or change working sessions.
7. App handlers must distinguish unsupported/disabled/denied/load-failed/empty, refresh after confirmed writes, preserve stale views with an error, and hold in-flight/uncertain mutations across navigation.

## Proposed bounded implementation scope (requires dot confirmation of Mesh semantics)

Client scope: `apps/console/src/api/{models,client,contracts,slotContracts}*`, direct-authority and App/FleetRuntime loader wiring, existing ServerSlots/Slot Details, codeVault and related tests. No navigation redesign.

Server scope proposed: `http/{managed_sessions,access_mesh}.py`, `application/{operator,access_mesh_operator}.py`, managed policy repository metadata (`storage/work_windows.py`), isolated Access Mesh policy-source storage/helper, policy-controller integration and narrowly scoped authentication/capability metadata only if needed. Tests use temporary databases. Existing OAuth bindings, Access number ownership, notification events, signing keys and deployed services remain untouched.

Proposed hierarchy: use the EXISTING Managed Fleet policy as the single Mesh-level authority, Standalone uses its local issuer/node defaults, and slot has explicit overrides or explicit inherited source. A member of a Mesh does not invent a competing issuer-local Mesh policy. Timing conversion must use existing after-start -> before-expiry semantics. No new global control plane.

Proposed migration: old slots remain `stored`/explicit effective snapshots until explicitly reset; newly created slots without overrides can inherit. Reads expose source, parent reference/revision, effective policy and supported reset capability. Reset is an expected-revision mutation, not a client-side value copy. Parent changes preserve overrides and apply to future windows/cycles without extending existing deadlines. Current-window/deadline adjustment remains a separate explicit action.

Open decision for dot: approve wiring the existing Mesh control policy into Access Mesh inherited defaults through its existing replication/application flow. Until approval, implement typed adapters/forms/tests and old managed source/reset support only; do not invent distributed policy semantics.
