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
| Whole-application legacy Actions route roster | `test_http_actions_schema` owns current operation ID and canonical task request binding. `test_http_auth`, `test_persistent_http` and `test_console_snapshot` exercise actual current HTTP operations. No assertion that native M3 routes are absent remains. |
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
