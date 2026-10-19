# Current-build atomic test audit

Audit base: `c968785` on `integration/M3-functional-candidate`.
Work branch: `test/halve-atomic-20261017`.

Before integration, canonical advanced through `e8957bd` (authorized namespace
transitions) and `f2396ff` (one current MCP catalog per role). Both changes were
merged into the work branch. Their two test-file conflicts were resolved here:
`joinPreflight.test.ts` checks current namespace admission and the managed-controller
fence as separate operations; `test_agent_object_contract` keeps current HTTP
validation. Remaining role and HTTP fixtures use the current Access Mesh setup;
the shared role double implements its intent/activity ports. Removed legacy-mode
and obsolete-description checks are not restored.

The requested outcome is fewer actual test cases and one current responsibility per
test. Every Python test module, the separate QA modules and every console test file
was reviewed. The test cleanup changes no application code or deployed configuration.
The canonical changes described above are preserved. Changes to
the existing runners update ownership and references to removed files; `full` still
runs all of `tests/` with its original exact-SHA and clean-checkout requirements.

## Counting and scope

| Corpus | Baseline | Current | Method |
|---|---:|---:|---|
| Python `tests/` | 2109 | 801 | Actual pytest collection, including parameterized cases |
| Separate Linux QA | 50 | 0 | Three duplicate historical acceptance modules removed; owners below |
| Native Windows QA | 25 | 11 | Static expansion of fixed parameter tables; requires native Windows to execute |
| Console | 456 | 188 | Dynamic Vitest collection/execution, including `each` cases |

Vitest's default static `list` reports declarations, not expanded parameterized
cases. Its original 411 declarations are therefore not used as the console case
baseline. Dynamic collection uses `vitest list --staticParse=false --json=...`.
No skip, xfail, test-name filter, reduced `testpaths`, hidden exclusion, benchmark or
loop of unrelated assertions is used to achieve the reduction. Diagnostic scoped
runs are reported separately from complete acceptance.

The per-module inventory is in [test-scope-inventory.csv](test-scope-inventory.csv).
It records case counts, removed/renamed names, current names, decisions and owners.
Python names identify definitions; console names include dynamic parameter expansion.
A retained name can contain a rewritten body; counts alone do not imply that its
old workflow survives. Git's diff records the exact removed and replacement bodies.

## Review rule

A test prepares a known state, invokes one operation and checks that operation's
result or durable effect. Reading storage to verify a committed write, reopening a
store to verify durability, and two competing calls to verify one concurrency rule
are legitimate oracles. A sequence that independently creates, starts, executes,
reads, changes policy, restarts and ends a session is not one test responsibility.
Neighboring transports and runtime ports are doubled. Storage owners keep their own
real SQLite transactions. Native process ownership and platform protection remain
explicit native checks.

Versioned names were checked against current callers before removal. Fleet's
current wire protocol, active managed-session permits, SQLite identity seals,
current policy inheritance and structured error paths remain covered. Historical
schema migration, old agent admission and import-alias compatibility are outside
the requested current-build scope. Negative checks remain where they verify a
current functional refusal, authorization fence or rollback; assertions that merely
prove a retired API or UI artifact is absent were removed.

## Current owners of removed scenario obligations

| Removed or simplified family | Current responsibility and owner |
|---|---|
| Old `test_agents`, `test_agents_native`, session restart and private access-code command flows | Current provider/principal admission: `test_managed_session_gate`, `test_managed_identity_authority`, `test_managed_session_application`. Window continuity: `test_work_window_store`. Actual execution: `test_execution_port_native`. |
| Auth/schema adoption, v0/v24 upgrade fixtures, old OAuth anchor QA | `test_identity_isolation` verifies a fresh seal, a missing installation anchor and a foreign database. `test_access_identity_guard::test_existing_runtime_refuses_missing_protected_auth` verifies current startup refusal. `test_access_authority` keeps missing/corrupt key handling. No historical migration is needed to prepare these states. |
| Whole-application legacy Actions route roster | `test_http_actions_schema` owns current operation ID and canonical task request binding. `test_http_auth`, `test_access_slot_native_http` and `test_console_snapshot` exercise actual current HTTP operations. No assertion that native M3 routes are absent remains. |
| Full OAuth registration → authorization → exchange → refresh → restart workflows | `test_http_auth` has individual registration, authorization-code, valid exchange, PKCE refusal and refresh checks against the current OAuth router and its own store. Refresh concurrency belongs to `test_oauth_store_refresh`; device exchange and revocation belong to `test_pairing_exchange`. |
| Window start/restart/extend/end chains, many-window recovery stress and duplicate public QA | `test_work_window_store` owns restart within the same window, concurrent start, policy snapshots, provider binding and queued-command blockers. `test_window_recovery` owns one due claim, no-due result, lease exclusion/reclaim and expiry race. `test_access_mesh_session_numbers` owns reservation/collision/reuse. |
| Notify → ACK → reply → expiry → restart message scenarios | `test_access_mesh_message_storage` owns notify read receipt, idempotent ACK, alert reply obligation, reply commit/rollback, expiry and pruning separately. `test_local_message_sender_receipts` owns the aggregate sender projection. `test_access_mesh_messages_v2` owns current send/target/discovery behavior. |
| Multi-node identity/number/replication workflows | `test_access_mesh_grants`, `test_access_mesh_outbox`, `test_access_mesh_replication_http`, `test_access_mesh_snapshot_gap` and `test_access_mesh_session_numbers` each verify one grant, event, ACK, gap or registry operation with injected neighbors. |
| Command run/read/cancel/retry workflows and old `v08`, `v09`, `v010` snapshots | `test_command_committed_receipts`, `test_durable_cancel_intent`, `test_cancel_atomicity`, `test_command_run_fast_path`, `test_command_scope_atomic` and `test_output_retention` own acceptance, commit boundaries, fencing, queue routing, task scope and UTF-8/total output bounds. Repeated RPC IDs are checked once in `test_command_replay`. |
| Persistent create/start/drain/rearm workflows and concurrency timing assertions | `test_persistent_lifecycle` owns each lifecycle mutation, `test_persistent_b_fencing` and `test_persistent_fleet` own the current permit/attachment fences, and `test_persistent_idempotency_storage` owns committed replay/conflict/pending outcomes. Console projection uses a small fixture and checks its values. |
| Old TaskDoneRequest, legacy-session claims, task history/review scenarios | `TaskDoneRequest` is excluded from the active `TaskRequest`/action catalog, so its old completion workflow is retired. `test_task_current_storage` uses current logical-agent ownership. `test_task_review_candidates`, `test_task_create_request_receipt`, `test_task_qa_public_state` and `test_compact_tmcp05_completion` keep candidate binding/freezing, committed receipts and current review feedback. |
| Event Bravo3 acceptance chain, callback/session/revocation/restart flows | `test_mcp_event_subscriptions` has direct owners for callback denial, identity recheck after callback, binding-write rollback and credential-revocation serialization. `test_mcp_events_managed_storage` owns private expiry and retained owner-removable binding after delivery revocation. `test_mcp_event_mesh_storage` owns replication and delivery persistence. |
| Page traversal, repeated wire variants and broad contract cross-products | `test_read_public_boundaries` checks current AccessMeshMessaging preflight and only the returned page's surfaced receipts. `test_read_backend_pagination`, `test_read_contract`, `test_read_wire_regressions` and `test_task_history_cursor_stability` own cursor/page rules. Current role catalog/output/error owners remain in the compact and MCP contract modules. |
| Real Fleet multi-node TCP, WAL/fanotify stress, retired identity replication and rollback flags | Current control decisions belong to `test_fleet_control_storage`/`test_fleet_control_hardening`; current source/outbox/query projection to `test_fleet_v1_source`, `test_fleet_v1_source_query`, `test_fleet_v1_projection` and `test_fleet_storage`. One store/operation and injected transport replace the full topology workflows. |
| Backup migration and nested pytest/preparation benchmark scenarios | `test_backup_manifest` verifies current runtime/auth/anchor snapshot contents. `test_test_workflow` verifies the existing selector behavior directly. Existing immutable schema/empty-database preparation remains active and is exercised by retained behavior tests; no nested test-suite benchmark is retained. |
| Windows app roles/start/attach/execute/end/restart scenario and repeated invalid-value matrices | `qa/windows/test_execution` keeps Unicode/exit status, one Job Object child-tree kill, assignment failure, PowerShell failure, capture cancellation and PID observation. `test_execution_port`/`test_execution_ipc` own portable admission/fencing/bounds. Native ACL and deployment-function checks remain under `qa/windows`. |
| Console old-server fallback, historical parity fixtures, M1 credential migration and obsolete slot controls | Current v2 inheritance decoding remains in `api/nativeInheritanceParity.test.ts`; the historical native runtime parity file is removed. Current slot API, code generation, pending-operation journal, provider credentials and selection state have direct owners. Unused `SlotPolicyControls` is removed from the test suite after checking callers; active `ServerSlots`/`AccessMeshPolicyControls` behavior remains covered. |
| Console render catalogs, CSS/source-text checks, repeated full connection/lifecycle and latency tournaments | Current UI actions remain in their route/component tests. Direct cache, projection, registry, realtime and selector tests own their state transitions. Functional deadlines use a controlled scheduler; there are no wall-clock performance thresholds or synthetic network tournaments. |

## Validation boundaries

The acceptance command is `python scripts/test.py full --expected-sha <sha>` in an
isolated `.[test,compact]` environment on Secondary. Console acceptance is its full
Vitest command plus TypeScript/build and lint. Reports identify the exact tested
source and remain outside the worktree. The final task handoff records actual test
counts, outcomes and elapsed time for the merged source. No historical benchmark
is used as a before/after timing comparison.

Secondary is Linux. Native Windows tests can be inspected and statically checked
there, but their Job Object/ACL/PowerShell execution requires Windows. This audit
makes no claim of native Windows execution or deployment acceptance. Deployment
approval and runtime rollout are separate from the authorized branch merge/push.

## Unified Mesh installation checks

The explicit clean-deploy operation introduces a separate state-reset responsibility.
Existing retention checks cover pruning old releases/backups and do not authorize or
exercise a full reset. `test_deployment_clean.py` covers removal of each managed
state area, external auth database sidecars, preservation of unrelated files,
pre-mutation path/link validation, repeatability, and rejection of retained state
during a fresh install. The existing failed-stage installer test now includes
`clean-deploy` and verifies that failed preparation preserves the prior state.
Windows XML/path checks and existing installer checks are reused.

The Console Join preflight check for a controller with members now accepts the
paired application's request. The server owns transition safety and registry
preservation; membership history and namespace names are not client admission
conditions. Existing connection lifecycle/cache-isolation and i18n checks are reused.


## Unified Mesh: primary ACS registry

The current Mesh registry is authoritative on its primary. New component module
`test_access_mesh_registry_storage.py` checks durable scope fencing, local erasure
without source revocation, exact registry handoff and historical issuer acceptance.
New integration module `test_access_mesh_registry_http.py` reuses the existing
two-node transport fixture for four atomic boundaries: forwarding issuance,
hydrating attach from the primary, namespace rejection and primary unavailability.
Existing grant, replication and lifecycle checks remain the regression owners.
Secondary scoped validation: 25 passed; Ruff and diff checks passed.
This source stage is not a deployment or final architecture acceptance receipt.

## Unified Mesh runtime, policy and membership foundation (2026-10-17)

Access is always available. Execution/Coordination expose attach and intent only;
the former /mcp catalog and mode-selecting validators are removed. Node identity
bootstrap runs without creating Mesh membership. Fresh-install identity validation
runs before storage constructors so retained auth cannot be silently replaced.

The old decentralized number-negotiation modules test_access_mesh_negotiation_contract
and test_access_mesh_pinned_allocation were removed with their retired algorithm.
Primary single-writer behavior, primary-offline denial and cross-namespace isolation
are covered by test_access_mesh_registry_http. test_access_mesh_unified_migration
checks exact slot/attachment preservation and common Mesh policy ownership.

New atomic checks cover node identity, topology admission fences and universal
installer ingress. Clean reset additionally checks external node-identity state.
Membership checks cover the source authority, not merely a lagging target replica.
The combined runtime, ACS, topology and deployment selection passed 126 tests.
This stage does not claim completion of the following primary-handoff and hot
migration implementation; full acceptance is recorded on the final source commit.


## Authoritative registry upgrade and namespace fences

The test_access_mesh_registry_upgrade module owns the one-time union of
registrations issued by different old members, preserving inactive rows and
historical issuer identity. It separately checks collision/offline refusal,
durable export retries, message-content archiving, removal of departed native
authority, same-Mesh return identity, and the SQLite fence for a request that
began before Move. These are direct storage/service responsibilities rather
than another full workflow matrix.

The integrated stage passes 35 selected tests, including the registry HTTP
adapter, native lifecycle, strict proof gate, node identity and the operator
slot-policy continuation. This is stage evidence; final full-source acceptance
is recorded after runtime and topology integration.


## Numeric-only Access and role entry points

The old merged-inbox-presence test selected a decentralized number winner.
That behavior is removed. The registry upgrade tests preserve both colliding
source rows and require explicit conflict resolution instead of selecting a
winner. Current registry HTTP tests verify shared end through a member Access
and deny a number-end request from the former primary after a handoff.

Native lifecycle, operator HTTP, intent freshness, public names and event-end
fixtures now use start/end on Access and numeric attach on roles. They obtain
native identity through the current registration, without the removed issuer/code
or verbose legacy session response. The integrated selected set passes 42 tests.


## Paired topology, primary handoff and durable Move

Application topology mutations and enrollment require an active paired OAuth
device with read and execute scopes before the configured general auth mode.
The new test_mesh_pairing_boundary component module covers paired, unpaired,
revoked, read-only, static bearer and auth-mode-none admissions. The integrated
auth boundary selection passes 24 tests.

The topology/registry integration selection passes 80 tests. Primary handoff
checks whole-registry digest/certificate validation, stale replay rejection and
app-designated succession. test_mesh_primary_handoff and test_mesh_topology_upgrade
are component modules; test_mesh_move_boundary and test_mesh_topology_recovery
exercise the real multi-router seams and remain integration modules. Existing
source-registry, policy and message modules retain their coverage.

Move uses one-use app enrollment independently of peer ingress, durable prepare
and completion receipts, and authoritative source activity checks. Tests include
lost prepare replies, blocked busy admission without a stranded source fence,
retry after ingress rotation, large registry payloads and one-time conversion
of a single populated historical topology. No old multi-authority runtime is
retained when a historical topology requires explicit resolution.


## Console uses one native Mesh contract

The complete Console suite passes 186 tests in 69 files. ESLint and production
build pass. The only build advisory is the existing large-bundle warning.
Removed ManagedWorkSessions, SlotPolicyControls and confirmedWrites tests belonged
to the retired persistent-session HTTP/UI path. Native Access policy, slot
mutation continuation, cache scope and local projection modules retain those
current responsibilities. Primary handoff and membership readiness checks cover
a paired follower entry, aborted-operation retry and a changed target controller
while retaining the app-selected Mesh identity.


## Final native runtime retirement and recovery boundaries

The old managed/persistent role-session modules are removed with their runtime:
application_operator_mesh, fleet_route_first_contact, managed_fleet_session_routing,
managed_identity_authority, managed_session_application, managed_session_gate,
managed_session_policy, managed_session_runtime, persistent_backend_admission,
persistent_console_projection, persistent_fleet, persistent_fleet_messaging,
persistent_lifecycle, window_recovery, work_window_domain and work_window_store.
The unused agent_test_helpers fixture is removed. Command replay/receipts/cancel,
task ownership/provenance, claim leases, message atomicity, application transactions
and auth-key preservation use native fixtures and retain their current assertions.

New runtime migration checks cover exact original IDs, epochs, deadlines, one-time
receipts, no reimport after Move and refusal of ambiguous active work. The actual
hot application lifecycle, fresh DDL, native Console agents and readiness remain
in existing current component/integration owners. test_mesh_runtime_migration
and test_deployment_offline_migration include real composition seams and remain
integration modules. Narrow row-conversion checks are component coverage in
test_deployment_migration_preservation.

Projection namespace checks cover all seven derived tables, unchanged application
storage and replay without wiping newly rebuilt data. Worker checks cover a solo
node without network polling, follower-to-Standalone and follower-to-new-Mesh
cache recovery, plus cancellation of an old in-flight refresh at the transition
boundary. The preserved-binding upgrade admission regression tests both primary
and member. Scope mappings remove only references to deleted test modules.


## Unified Mesh: full-gate fixture alignment

The former per-agent Mesh revoke/drain facade and its route epochs are retired. The three tests in `test_mesh_session_authority_review.py` are removed because current authority is covered at the actual registry and signed topology boundaries:

| Retired assertion | Current owner |
| --- | --- |
| A non-authority peer cannot revoke a foreign session | `test_access_mesh_replication_http::test_authenticated_peer_cannot_spoof_event_issuer`; `test_access_mesh_registry_http::test_previous_primary_cannot_commit_cycle_after_handoff` |
| A stale authority epoch is rejected | `test_mesh_primary_handoff::test_old_primary_certificate_cannot_reclaim_a_later_authority`; the previous-primary registry test above |
| Current authority may finish the session | `test_access_mesh_registry_http::test_member_acs_end_closes_the_shared_cycle`; `test_access_mesh_replication_http::test_http_delivery_applies_committed_event_and_acknowledges_outbox` |

`test_persistent_http.py` is replaced by `test_access_slot_native_http.py`: both operator create/delete scenarios now use `/actions/access/mutate`, assert the native numeric identity and verify that deletion retires the registration and number. `test_http_actions_schema` binds the current `/actions/tasks` request schema and rejects reappearance of the retired mutation route. Task ownership fixtures create a real native work session before claiming its lease.

The full runner now gives each test its own fallback state paths. Explicit test configuration, production node-identity checks, migration checks, full-source SHA validation and zero-skip evidence requirements remain intact. Mesh trust, VPN and control test doubles implement the current namespace and transition ports; they do not relax production checks.


## Native admission at the final write boundary

Commands, tasks and messages capture the current registration namespace before
asynchronous preflight and recheck native session authority in the same SQLite
write transaction. Command launch checks durable attribution again before stdin.
These checks use the current shared policy window; they do not add a second
session lifecycle or turn durable task ownership into an expiring lease.

| Obligation | Current test owner |
| --- | --- |
| Queued and recovery command writes after end/Move; PID admission and original namespace | `test_persistent_b_fencing.py` |
| Late task claim after completed end cleanup or Move; unchanged durable claim with `release_on_end=false` | `test_task_mesh_policy_receipts_v2.py` |
| Late send, reply and normal ACK after end; send and expiry ACK after Move; allowed expiry ACK and draining finalization | `test_native_message_admission.py` |
| Queue worker submission cannot inherit the first caller's admission context | `test_command_run_fast_path.py` |

The new message module crosses the real application/admission/storage boundary
and remains integration coverage, owned by Mesh and events. The shared admission
and policy-window source files declare their consumers and deferred integration
boundaries. Full acceptance still runs the complete collected suite without
selection, skips or legacy API restoration.

## Membership activity blockers retired

The former `mesh_active_work` refusal and source-activity admission checks are
retired with the updated membership contract: slots, sessions, queued/running
commands and cleanup no longer prevent Join, Detach, Move or primary handoff.
The existing refusal cases now check successful active-work transitions.
The conflicting-transition recovery case retains its current error boundary. Commit-time namespace fencing, local cleanup, source
registry preservation and primary-handoff recovery remain current obligations.

The former lagging-replica activity-refusal case duplicated the retained member-
departure/source-registry owner after removal of the refusal and was deleted.
Registry admission now checks each activity kind independently through one
parameterized storage case; active and suspended snapshots reuse the existing
handoff and departure cases. Native authority retirement remains a local-store
check, and existing HTTP attach coverage verifies lazy primary lookup.

## Pairing link V1 retirement (2026-10-18)

The pairing URL contract is now version 2 only, with a single code field
and the existing server/name/optional profile metadata retained. Old version 1
links and the former exchange request key are intentionally unsupported.
Existing Console pairing fixtures are updated to version 2; the only new
version-1 check is an atomic negative parser case. The pairing store's
one-time and expiry rules remain unchanged.

## Console connection link naming (2026-10-18)

The current operator/agent CLI uses the one-time Connection Link terminology:
the issuing subcommand is 'terminal-mcp link', the clean-deploy opt-in is
'--reset-connections', the protected output file is 'link-url.txt', and
installer stdout says 'Connection link'. The previous command, flag and file
name are deliberately retired without compatibility aliases. Existing CLI,
installer and protected-file tests now assert the new surface. An atomic CLI
help test guards against reintroducing credential-sounding terminology.

Device registration, one-time code verification, the HTTP exchange protocol
and OAuth retention/reset boundaries have not changed.

## Slot/session separation and current schema transition (2026-10-18)

Slots now retain a number and policy; the separate session owns the agent identity
and explicit end marker. Reopen uses the original end plus cooldown, including an
early resume followed by a later full window. The projection reports only an
actually opened window. The existing grant, future-policy, number and registry
tests were updated at their owners to match this contract.

| Retired assertion | Current obligation and owner |
| --- | --- |
| `test_access_mesh_unified_migration::test_temporary_kind_migration_preserves_slot_and_attachment` converts an obsolete `legacy` slot schema | The supported data-model transition is the deployed v26 to v27 split. `test_access_mesh_session_schema` preserves live identity, attachment, original deadline and task data while physically removing deleted slots. |
| `test_access_mesh_session_numbers::test_recycled_number_forms_new_identity_epoch_and_retains_old_history` reuses digits after a deadline while retaining the old slot and its sessions | A slot reserves its number until physical deletion. `test_access_mesh_slot_storage` checks the reservation after expiry and the physical native-session/binding/number cascade on deletion. |
| `test_access_mesh_future_policy::test_policy_capture_uses_current_automatic_cycle` silently advances a projected cycle without a start event | Its updated case `test_policy_capture_keeps_last_actual_window_until_start` retains the actual window. Explicit and automatic start eligibility use the shared reopen/cooldown helpers. |

The current `test_mesh_parent_replaces_a_previous_node_default_override` was moved
from the removed mixed migration module to `test_access_mesh_registry_storage`.
Its Mesh inheritance obligation remains intact.

New storage cases cover gaps absent from the previous suite: empty manual-slot
identity, deletion of linked native work sessions, the exact 72-hour boundary for
both slot kinds, a long session's deadline as its latest possible use, and the
transactional retention recheck after a concurrent touch. Natural expiry verifies
the original deadline as the native end timestamp. Policy-boundary cases reuse the
existing core module and parameterize slot kind, Rearm and a captured policy edit.
These are isolated owning-component checks; broader runtime wiring remains in the
existing explicit integration layer.

Additional atomic cases cover issuer-revision ordering when clocks tie or reverse,
a stale number snapshot after an applied end, zero-cooldown reopening, terminal
deletion at missing or gapped replicas, stale-delete protection, late recipient
holds after deletion, consistent slot/session snapshots, and suspended-slot
retention. The obsolete legacy-receipt-kind conversion test is removed; it
asserted a retired mode rather than the current admission switch.

### Slot/session owner fixture follow-up

- `test_subscription_hold_fences_slot_deletion` now checks physical deletion,
  release of the recipient hold and number, and retention of the historical
  subscription recipient. A hold no longer prevents deletion under the current
  slot contract; recipient challenge rechecking still protects immutable identity.
- `test_hot_control_scope_conversion_preserves_existing_acs_registry` is replaced
  by `test_control_reinitialization_preserves_existing_acs_registry`. The artificial
  rollback to control schema 4 and conversion of `old-domain` are retired. The
  current obligation remains: reinitializing FleetControl and applying its policy
  preserve the Mesh namespace and the complete slot projection, including session
  identity and policy. The common recovery fixture supplies the required creation
  time; the other recovery and authority handoff cases remain.
- Existing rotated-code and subscriber fixtures use session-owned identity. The
  native message admission checks are unchanged and verify the repaired lookup of
  session identity through a number claim's immutable slot reference.

## Console slot editor stability (2026-10-19)

The common slot editor reads policy on entry and on explicit operator actions.
Background slot polling does not clear, disable, or replace the policy draft.

| Retired expectation | Current obligation and existing owner |
| --- | --- |
| A server workspace has no Slots navigation button. | `AppShell.test.tsx` sends its Slots button to the common `/slots` selector. |
| A failed refresh or an expired observation disables all mutations and displays a transient inline freshness warning. | `ServerSlots.test.tsx` retains known rows, the form and available actions. The action itself reads current metadata; `accessMeshState.test.ts` and `accessMeshReadIsolation.test.tsx` keep policy reads independent from connection churn and slot polling. |
| A newer backing revision invalidates an edited form and requires discarding its draft. | `ServerSlots.test.tsx` and `MeshSlotPolicyTransitions.test.tsx` send the complete captured draft with action-time CAS. A definite revision conflict permits one retry; uncertain requests are not blindly replayed. |
| A changed parent revision invalidates an already opened inheritance confirmation. | `SlotWindowAuthority.test.tsx` preserves the reset intent. `ServerSlots.test.tsx` binds the actual mutation to fresh slot and native-defaults revisions. |

`SlotAuthorityTransition.qa.test.tsx` retains actual pairing/registry fences and
immutable persisted recovery. Its fixtures trigger a registry change through an
explicit control read, since replacing a loader callback no longer implies that
the server has changed its registry. These are isolated component consumers with
injected transports; no full or integration test workflow is added.
