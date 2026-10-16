# Test scope cleanup: independently reviewable candidate

Base: `fe66ea6317e4c951b22210d665041341f91843e1`, branch
`codex/atomic-test-selection`. Runtime, installer, release approval and frontend
sources are outside this change. The capture candidate and its acceptance do not
depend on this branch.

The user approved removal of previous compatibility guarantees and atomic
component responsibility. This is a bounded migration: reviewed component modules
form the fast allowlist; remaining mixed modules stay visible in integration or
platform batches. A versioned filename or the word `legacy` is insufficient proof
that a currently supported behavior is obsolete.

## Selection results

The counts and original capture-based selectors below are historical evidence for
the original packets. The M3 transfer notes at the end describe the current branch.

Counts below come from pytest collection, not full test execution. Parameterized
cases are retained. The Windows inventory used import-only `pwd`/`fcntl` placeholders;
it is not Linux/native acceptance evidence.

| Selection | Before cases | Candidate cases |
|---|---:|---:|
| All Python tests | 2950 | 2959 |
| Diagnostic capture component | unavailable | 15 |
| Diagnostic capture integration | manual file list | 28 |
| Events component | 1863 unique | 40 |
| Mesh component | 2154 unique | 79 |
| Reviewed component checks, all owners | unavailable | 1024 |
| Explicit Linux/platform batch | implicit within full | 213 |
| Documentation-only affected selection | 2950 | 41 |

There are now 1781 functions in 244 modules, versus 1763 in 239. Full cardinality
increases by nine cases because selection safeguards and separate current
obligations were added while prior snapshots were removed. Reducing the number of
relevant parameter variants is not the optimization target.

Paths are canonical POSIX strings on every host. Before the change, component
Events on Windows selected four default files twice, adding 398 repeated cases.
The repeat was verified by collection: the same public-error module passed through
two path spellings collected 712 instead of 356 cases. The candidate's selectors
have no such repeats.

`component` uses direct owner patterns and reviewed component checks. `integration`
explicitly expands the existing dependency graph and includes the selected
component checks. `platform linux` selects declared shell/Unix IPC/kernel checks.
Mixed-function declarations preserve pure checks in compact/release/diagnostic
modules while keeping app/native cases explicit; stale declarations block a plan.
Unreviewed modules default to integration.

`--changed-from` uses declared source owners and transitive helper consumers.
Unknown/shared changes select all reviewed component checks and show deferred
integration/platform coverage. They do not start a complete release run. Source
ownership is staged feedback, not proof of exact runtime impact. `full` continues
to run every Python test, without selectors, and remains the only mode eligible
to create full-test evidence. The release approval validator is unchanged.

## Removal and replacement ledger

| Previous scenario | Evidence/purpose | Current obligation and destination |
|---|---|---|
| `test_application_boundary::test_previous_projection_imports_remain_compatibility_aliases` | Asserted identity of the previous private import path and the canonical function. Pure old import guarantee. | Current projection values, field bounds and history isolation remain in `test_task_projection_core`. No alias-equivalence promise is asserted. |
| `test_task_projection_core::test_legacy_import_is_same_canonical_type` (10 cases) | Compared transport re-exports against core types; no current projection behavior was checked. | Core tests now import canonical types only and validate their current projections. |
| `test_task_projection_core::test_all_seven_legacy_discovery_contracts_preserve_baseline` | Constructed all MCP tools inside core tests and froze historical SHA256 values. | Current catalogs, schema bounds and outputs are checked in `test_mcp_output_contracts`, `test_mcp_input_bounds`, `test_compact_contracts` and `test_documentation_contract`; historical hash equality is retired. Its private fixture was removed. |
| `test_http_application_contract::test_http_compatibility_signatures_survive_application_cutover` (7 cases) | AST signatures were compared to Architecture A baseline `7183a65`. It did not exercise current HTTP bodies, auth or responses. | Current OpenAPI metadata and auth stay in `test_public_operation_metadata` and `test_http_auth`; console/persistent/fleet route tests remain in explicit integration/platform acceptance. The snapshot-only file and fixture were removed. |
| `test_application_boundary::test_architecture_a_preserves_exact_published_mcp_contracts` | Historical MCP hashes plus a current caller-identity boundary in one test. | Old hashes and their fixture were removed. `test_mcp_application_adapter::test_adapter_inputs_never_expose_server_resolved_identity_fields` retains the current security assertion. |
| `test_live_claim_projection::test_task_context_timer_has_canonical_name_and_compatibility_alias` | Asserted old timer aliases and used real service/storage plus a sleep for freshness. | `test_agent_context_freshness` uses AgentCoordinator, a mocked store and fixed clocks: policy, exact TTL, expiry and missing context (5 cases). |
| `workflow['active_claims']` assertion inside `test_expired_owner_stops_projecting_before_persisted_claim_cleanup` | Explicit compatibility alias for unreleased rows. | Current unreleased/live/stale claim counts, ownership and data-preservation assertions are retained. No whole current claim test was deleted. |
| `test_access_mesh_messages_v2::test_fleet_commits_local_first_deduplicates_and_preserves_identity` | Current `message` rejects fleet scope before dispatch; the old commit/replication scenario is unreachable. | One application-port test rejects fleet scope before resolution, storage or replication. |
| `test_access_mesh_messages_v2::test_large_fleet_envelope_is_rejected_before_local_or_peer_commit` | Same early `feature_disabled` rejection, with an ordinary short string rather than an oversized envelope. Duplicate obsolete framing. | Covered by the same fleet-scope application guard; current storage receipt overflow retains a separate real rollback test. |
| `test_access_mesh_messages_v2::test_lost_ack_duplicate_delivery_and_same_request_have_one_effect` | `drop_ack_once` was set while the test asserted zero peer calls. Current explicit outbound sends produce distinct IDs. | Application test checks distinct IDs for identical text/request ID. Storage tests separately check one effect for identical wire retry and conflict rejection for changed wire. |
| `test_access_mesh_messages_v2::test_public_send_returns_durable_receipt_within_total_peer_budget` (2 cases) | Both old flush-lock variants exercised a retired peer path. `flush` is a current no-op. | Application-port test checks local commit and zero peer scheduling/calls, without two databases or a wall-time assertion. |
| `test_access_mesh_messages_v2::test_offline_peer_retains_local_success_and_outbox_survives_restart` | Offline peer setup was unused; current behavior is local storage persistence. | Storage test reopens one current store and compares committed wire/receipt. |
| `test_access_mesh_messages_v2::test_oversized_peer_acceptance_proof_rolls_back_all_obligations` | Useful current transaction bound was exercised through a two-node application fixture under an old peer-proof name. | `test_access_mesh_message_storage::test_oversized_acceptance_receipt_rolls_back_message_and_recipients` exercises real current SQLite rollback with one database. |
| `test_application_boundary::test_mcp_adapter_accepts_canonical_application_directly` | Mixed application reuse, actor conversion and service-vs-application catalog parity. | Application reuse stays with Application. Actor creation and MCP inputs/catalog/handler ownership have separate adapter tests; old caller-construction parity is retired. |

Current application fallback denial and facade allowlist/context restoration are
kept as separate atomic tests. The facade is still invoked by real routes.
Current legacy error normalization is also kept: `tasks.py` still emits
`already_claimed`/`agent_busy`. Supported schema migration, existing slot/session
data and the deployed in-process topology retain their acceptance tests.

The remaining directed-recipient test was renamed to describe what it actually
checks: rejection of a remote-only recipient before local mutation. Dead offline,
lost-ACK and before-request fixture controls were removed after checking importers.

## Native relocation and atomic splits

The following ExecutionPort functions moved intact to
`tests/test_execution_port_native.py`, with AST-equivalent bodies/decorators:

- `test_native_replayed_spawn_and_input_execute_exactly_once`
- `test_native_output_reads_require_bounded_integer` (7 variants)
- `test_cancelled_capture_does_not_leave_owned_child`

All nine native cases remain in `platform linux` and `full`. Protocol model checks
remain component checks; eight service/scheduler/SQLite composition functions are
explicit integration selectors. The native polling helper is local, with no
cross-test import.

Task detail and receipt history isolation now run independently. Receipt owner,
warning-count and warning-byte constraints are separate cases. Current DTO size
checks no longer assert the prior raw-record compatibility promise. Actor adapter
identity and MCP handler ownership moved from application tests to their owners.
Receipt unit tests replace platform/load observation with deterministic ports so
testing receipt logic does not depend on the host's `ver` command.

## Reproduction and limits

Run the existing installed Linux test environment (`.[test,compact]`):

```sh
python scripts/test.py component events --plan
python scripts/test.py integration events --plan
python scripts/test.py dev --changed-from <base-sha> --plan
python scripts/test.py integration --changed-from <base-sha> --plan
python scripts/test.py platform linux --plan
```

Windows verification for this candidate uses direct scoped pytest, with an
import-only `pwd` placeholder where imported Linux modules require it. Component
decisions use mocked external ports; storage tests use temporary SQLite databases.
This is not evidence for Linux process execution or native rollout. The existing
profiling runner remains a Linux environment workflow.

Verified on Windows: 45 selection safeguards passed in 0.41 seconds; 104 scoped
Application/Actor/MCP/Core/Message/storage cases passed in 3.07 seconds before the
last added instructions-selector case; the exact Events component plan passed
40 cases in 3.13 seconds. The dependent local-message fixture checks passed as part
of the earlier 177 passing cases; two application-test relocation import failures
in that run were corrected and covered by the later green scoped run. Ruff and
whitespace checks passed for the modified Python sources.
Full execution, native relocation execution, frontend build and live rollout are
separate acceptance work and were not run in this task.

The complete collection inventory recorded 2958 cases before the final new
instructions-selector regression. A scoped collection of the current workflow
module verified all 45 cases and the one-case addition, giving the combined
2959-case index without running a complete suite.

The reviewed allowlist and explicit mixed-function declarations are an incremental
migration. Other mixed modules remain in the visible integration/platform batches;
they are not claimed to be entirely atomic. Future promotion to component feedback
requires separating their real boundaries first. No full receipt or release-policy
exception is fabricated to turn narrow diagnostic evidence into release approval.

## Follow-up packet from `f784db9`

The first packet remains unchanged on `codex/atomic-test-selection`. This child
branch fixes concrete affected-selection omissions and separates one current
policy responsibility; it adds no runner modes or runtime changes.

Before this follow-up, `storage/work_windows.py` missed its directly importing
`test_work_window_store.py` (CAS, principal isolation and rollback), and
`auth/middleware.py` missed `test_android_slot_read_scope_qa.py`. The existing AST
import graph now includes direct source-to-test edges before following helper
consumers. The four Android middleware cases use no app/process/store and are
reviewed auth component checks. Shared storage/auth/lifecycle contracts show all
non-platform/native boundaries; direct feedback remains separate. Unknown source
families are documented in `testing.md` and appear with a reason in each plan.

Three existing pure `decide_session_operation` functions moved from
`test_managed_session_application.py` to `test_managed_session_policy.py`. The two
28-operation parameter tables and function bodies are AST-identical. The third
test's three existing bad inputs now have separate pytest IDs instead of a loop:
57 collected cases become 59, with **zero new policy scenarios**. It imports only
the core policy/window values; application, authorization, storage, adoption and
recovery tests retain their original bodies. Closest WorkWindow tests check phase
thresholds; these reused checks cover the complementary operation-admission rule.

Five public Service health functions in `test_sqlite_main_health` and four real
runtime/journal/auth projection functions in `test_fleet_v1_source` are declared
integration instead of component. Their bodies remain unchanged. Storage integrity
and pure protocol/meta/router checks remain component selectors. Physical separation
of those mixed modules is the first remaining cleanup priority.

Collection only: 245 modules, 1786 functions, 245 parameterized functions and 2969
cases. The disjoint case partition is 1086 reviewed component cases,
1670 remaining integration cases and 213 platform cases. The integration mode
includes selected component cases, so those totals must not be added to an
integration plan that already includes them. Capture still selects 15 component
or 28 explicit integration cases; Events component stays at 40. The ten-case
increase since the first packet is two IDs for the existing policy inputs and
eight targeted selector regression cases, not additional behavioral variants.

Scoped verification: 59 reused policy cases passed in 0.19 seconds. Selection,
policy and Android scope checks together passed 116 cases in 1.65 seconds. The
updated documentation and source-route matrix passed 45 cases in 1.22 seconds. The
current whole inventory was collected without executing it (18.36 seconds).
Full/native execution and broad integration acceptance remain deferred. Windows
import placeholders are still not native acceptance evidence.

Backlog recorded at packet two (closed by the completion packet below):

| Priority | Module | Concrete split |
|---|---|---|
| 1 | `test_sqlite_main_health` | Repository integrity/cache/timeout versus five Service health/journal/coordinator integrations. |
| 1 | `test_fleet_v1_source` | Protocol, node-meta storage and router double versus persistent snapshot/event replay with real auth/runtime stores. |
| 2 | `test_http_auth` | Middleware path/scope, atomic OAuthStore refresh race, OpenAPI adapter and real command execution. |
| 2 | `test_managed_runtime_composition` | Two fake-recovery coordinator tests versus two real `create_app` wiring tests. |
| 2 | `test_mcp_events_managed_live` | Seeded event-store projection versus `test_actual_asgi_subscribe_no_auth_reports_stage_and_no_secrets`. |
| 2 | `test_agents` | Identity, preview, expiry and scope functions versus service/storage and actual bash behavior. |
| 3 | `test_persistent_b_fencing` | Settings thresholds and mocked admission versus real permit/store/fence/recovery. Pre-spawn cancellation alone is not a native process test. |
| 3 | `test_persistent_c_rollout` | Current admission policy, installer configuration preservation and application lifecycle/console wiring. |

Current admission/drain/adoption, migration and data-preservation branches still
exist in runtime. Their versioned/legacy names are not retirement evidence; no
such case was deleted in this follow-up.

## Completion of the eight-module backlog

The third packet starts from immutable `b0e81dc`; both earlier review branches are
preserved. All eight named modules are physically separated. This closes the agreed
backlog; other repository modules are not claimed to be entirely atomic.

| Original module | Reviewed component responsibilities | Remaining real boundary |
|---|---|---|
| `sqlite_main_health` | Repository integrity/cache/cancellation/real SQLite worker cleanup (10). | Four Service/journal/workflow checks; Service+symlink and repository+symlink checks. |
| `fleet_v1_source` | Protocol (3), own node metadata (4), fake-port router (2), Settings (3). | Four snapshots/replays across runtime, journal, metadata and auth stores. |
| `http_auth` | Settings (1), middleware paths/scopes (19), own OAuthStore refresh race (1), router/DTO schemas (15). | Twelve real HTTP/auth cases, two actual app OpenAPI cases, two native command/recovery cases. |
| `agents` | Identity/preview (5), expiry (2), scope (4), own AgentStore uniqueness (1). | Fifteen Service/storage/history/admission cases; eight actual bash/queue/cancellation cases. |
| `managed_runtime_composition` | Three recovery-loop lifecycle checks with neighboring ports doubled. | Two actual app composition/lifespan checks. |
| `mcp_events_managed_live` | Ten EventStore callback/dedup/authority/inactivity cases over its own current SQLite database. | One actual ASGI auth/diagnostic-redaction ingress check. |
| `persistent_b_fencing` | Settings thresholds (5), backend scope admission (1), own idempotency/schema transitions (4). | Nine store/scheduler/lifecycle/permit/fence cases with a forbidden native neighbor; one actual bash recovery+attribution case. |
| `persistent_c_rollout` | Five admission-policy alternatives, six installer/example declarations, one console projection with neighboring ports doubled. | Five actual HTTP/app policy/persistence/next-arm/existing-session-drain boundaries. |

Integration remains where propagation across the real boundary is the subject:
committed permit versus queue claim; session epoch versus materialization/end;
journal/database failure versus public health; authoritative auth identity versus
source replay; or route/lifespan policy versus persistence. Doubling those owners
would remove that boundary. Native recovery/run/read/cancellation requires the real
shell/process port. Target identity requires actual symlink replacement. These are
explicit integration/platform scenarios, not hidden component checks.

The existing MemoryExecutionPort is reused as a forbidden native neighbor in
non-process persistent wiring. Rule/value/schema checks do not create the real app.
Console enrichment retains the old 16 slots and output/concurrency assertions with
backend/task/audit ports doubled. Native files retain shell bodies and inherit only
their former preparation allowlist entries; migration/generator files are not cached.

Structural proof: 21 SQLite/Fleet functions are exact AST relocations; four compound
functions preserve every original assertion/exception check in separate duties.
Root's four modules retain 21 exact AST functions and seven more with only the native
constructor replaced by scheduler+forbidden port; seven others split old inputs and
obligations. The bearer function retains 95/98 assertions exactly; three coordinate
assertions use the same 160/161 inputs in parameterization. Actual app route/version/
consequential metadata, OAuth PKCE/revocation/credential authority and session/data
guards remain. The task-lease alias dependency is replaced by current context TTL,
used by core AgentCoordinator admission/expiry; stale/refresh behavior is unchanged.
No current scenario was removed in this packet.

Collection: 271 modules, 1833 functions, 254 parameterized functions, 3044 cases.
The 75 extra IDs since packet two are existing assertion/loop inputs: 51 auth/agent,
seven SQLite/Fleet and 17 root-owned cases. Partition: 1175 component, 1679 remaining
integration, 190 platform. Capture stays 15/28; Events component grows 40 to 50 by
exposing ten existing EventStore cases. QA/frontend remains separate as documented.

Scoped Windows results: auth/agent component 48 passed/3.57 s; SQLite/Fleet component
22/3.06 s; root component plus memory-port fencing 44/9.05 s; actual portable
Service/Fleet/OpenAPI integration nine/5.22 s; existing selector safeguards 53/1.15 s.
Complete collection only took 9.11 s. Full and all shell/symlink acceptance are deferred.

The broader 17-case root wiring probe yielded 11 passed and six failures before the
scenario assertions: actual AuthFoundation startup rejects Windows verifier-key mode
as insecure. Those six need Linux secure-file semantics. No mode/stat shim, auth
bypass or runtime patch was applied. Nine passing fencing cases overlap the 44-case
run and must not be added as independent coverage.

Review's helper-selector issue is closed: graph traversal remains; final targets are
intersected with actual test files. The existing direct-source/transitive-helper test
now traverses an uncollected conftest and checks proper targets in both layers,
without adding cases.

## M3-only integration transfer

Initial integration base: `b6b0605d893444ccce50089c6a865891fb63d8f8` on
`integration/M3-functional-candidate`. The target now also includes the separately
accepted PR #4 cleanup commit `1fe0a5c62db8d693680ce5bb10dbc6ac7c874b9e`.
It was merged into the feature branch without conflicts; the published history
is preserved. Cleanup on every outcome, explicit diagnostic retention and the new
selection guards remain together. The original fixed candidate
`b59a5d9af482edc8811c8368dc6785b64780ade5` is preserved on its original branch.
Only the three cleanup packets `f784db9`, `b0e81dc`, `5c4a943` and the existing
one-line `bearer_client` fixture fix were cherry-picked. There were no conflicts.
Runtime, frontend, installer and dependency files match the M3 base.

The separate `fe66ea6` subscription-capture change is excluded. Its runtime,
documentation and two dedicated test modules are absent on this branch. Dedicated
capture owner patterns, allowlist, integration extras and source-consumer edges are
deferred with that feature; no existing M3 behavior test is skipped or removed.
Events still exposes its current storage, app and subscription-commit boundaries.

Six existing workflow checks keep their selection/import/boundary assertions but
use the current Events/Read owners and documentation instead of capture fixtures.
The existing invalid-command parameter table adds one case to verify that the
deferred capture component is rejected. No capture runtime is mocked into existence.
Capture examples are replaced with current Events/Agents commands. The earlier
collection totals in this document remain historical, not M3 acceptance evidence.
Static current inventory: 269 test modules, 1818 test functions; 187 source files
(182 Python modules and five assets), with 114 owner routes and 73 explicit unknown
routes. No full suite or full collection was run for this transfer.

## Kilo-18 atomic-half audit (2026-10-16)

The test-only worker started at M3 revision
`502c093b6e79d7e4e8d3dece209c977a6b042768`, with 2,973 pytest cases. The current
candidate collects 2,525 cases: 515 baseline node IDs removed and 67 added, for
a net reduction of 448. This is substantive progress but remains 1,075 above the
requested 1,450 ceiling. No source, fixture, skip, xfail, deselection, or
test-selection rule was changed. Exact before/after node IDs and the delta are in
the Kilo-18 evidence directory.

| Removal group | Removed | Retained owner/property |
|---|---:|---|
| `test_public_errors::test_registered_codes_are_stable_bounded_and_serializable` | 257 | Replaced registry-wide serialization repetition with ten representatives covering catalog kinds and distinct access recovery overrides (`stop`, `start_session`, `session_expired` return-to-chat). Each retained case checks class/recovery, serialization, size, and round trip. The source-code scan still requires every owned public code to be catalogued; component owners retain domain mappings. |
| `test_public_errors::test_deployed_public_code_survives_legacy_normalization` | 18 | Generic registered-code preservation and unsafe-detail discard are covered by legacy normalization and fail-closed unknown-code tests. Named domain codes remain under their component owners. |
| Managed-session policy operation tables | 57 | Three draining rows represent read/finalization/new-work branches; four expiry rows represent the two permitted and two denied classes. Exact operation classification sets and the cooldown phase have separate tests. |
| `test_task_mcp_contract::test_every_variant_rejects_unknown_fields` | 11 | One claim variant checks the shared strict request base; generated action schemas still enforce closed fields for every variant. Per-action accepted/required fields remain tested. |
| Task ownership v2 state/reason matrices | 41 | Claim/release keeps unleased-active and leased-terminal representatives. Cleanup keeps active and terminal states for one reason, including provenance/data preservation and idempotence. Property updates keep active/terminal representatives; the shared request model rejects one forbidden state value. Distinct expiry/fencing/successor-owner and durable preservation checks remain. |
| Compact B1-B13 regression assertions | 13 | Removed checks now owned by task request/action schemas, runtime task owners, and the modified minimal `relate` request. Retained B2 source-backed action coverage, B7 role-specific nullability, B9 non-null runtime requirements, and B10 omission versus empty-array semantics. |
| Compact close/incoming/error-notice cross products | 47 | Explicit pairwise rows retain chunk mode/profile, command/recovery paths, refusal action/mode, operation/mode, and length/Unicode boundaries. Duplicate overflow recovery scenarios were covered by durable SQLite receipt and actionable refusal owners. |
| Compact response checkpoint and health tables | 7 | Health keeps healthy/full and unhealthy/minimal representatives. Checkpoint snapshots retain object, empty object/list, nested falsy values, ordinary/JSON-like/empty strings. Removed repeated JSON-looking strings whose decoded type is still string. Read notice and run schema duplicates have compact incoming and MCP output schema owners. |
| Task request boundary/type repetition | 8 | Unsupported checkpoint scalar uses integer and boolean representatives; common unknown-field, release backend-deferral and ToolManager parity checks each retain one representative. Detailed `tool.run` error boundaries and completion-field allowlist cases remain. |
| Provider identity missing-metadata and invalid-value tables | 21 | Missing metadata retains empty object, public-field-only, auth/public fallback, control-field fallback, and wrong-container representatives. Each required identifier retains empty, wrong-type, and structured values. Positive metadata, namespace/digest and oversize/control/Unicode checks remain. |
| Remaining public error value tables | 26 | Deployment failure phase/suffix checks retain one phase with three suffixes because catalog policy is generated identically for phases; exception classes in the same mapping branch are grouped, and duplicate invalid revision/retry values are removed while retaining type and bound classes. |
| `test_compact_failure_budget::test_oversized_accepted_recovery_preserves_receipt_and_inbound_action` | 1 | The close module retains a durable SQLite receipt check for combined large output/incoming content; alert action refusal is covered by current error notice and command refusal tests. |

The audit is not exhaustive: the current count is above target, and remaining
high-cardinality modules still need owner-by-owner review. No compatibility
guarantee is removed based on a filename: the README-supported legacy slot kind
and `/mcp` endpoint remain current. The retired fleet-scope message transport
remains rejected by its current local-only guard tests.


## 2026-10-17 follow-up: shared owners and smaller scenarios

Base canonical: `4334540`. The previous feature tranche `a65b32e` had
2,164 collected cases. This follow-up has **2,145**: 19 fewer than that
tranche and 22 fewer than the 2,167-case canonical baseline. Compared with
the original 2,973-case acceptance baseline, the 1,486-case target remains
**659 cases away**. This is a partial cleanup, not completion of that target
or a claim that all remaining scenarios are atomic.

The command remains `PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/python
-m pytest --collect-only -q` (one shell command, including parametrized cases).
No test discovery rules, skip/xfail markers, production code, fixtures,
dependencies, or platform code were changed by these cleanup commits.

| Removed repetition | Retained property owner |
|---|---|
| Four generated task-schema/example tests | Strict action matrix and exact FastMCP runtime/schema parity in `test_task_mcp_contract.py`; action-specific requirements and rejected runtime inputs remain. |
| Three byte-identical documented JSON inputs | One identical input for the same model remains in `test_documentation_contract.py`; distinct JSON/model inputs still collect separately. |
| Repeated Executor/Coordinator session-schema checks | `test_three_effective_role_contracts_match_reviewed_manifest` pins all role schemas and annotations. |
| Stale-authority app example | `test_gate_rechecks_current_slot_authority_in_same_snapshot[authority_epoch-2]` retains the actual gate invariant. |
| Four per-module import checks | `test_application_modules_never_import_inbound_transports_or_sql_drivers` covers the complete application package. |
| Synthetic cancellation raised from a transaction body | `test_actual_task_cancellation_rolls_back_and_releases_writer` cancels a real task, proves both context and activity rollback, and verifies a subsequent writer. RuntimeError rollback remains independent. |
| Repeated nonfinite busy timeout | NaN still exercises `math.isfinite` rejection, with separate finite bounds retained. |
| Pairing source grep and repeated CLI journey | Actual list/revoke CLI, absent remote pairing surface, credential invalidation, and existing database bytes/mode preservation remain with focused owners. |
| Repeated code-only version change | Code-only version increment and independent schema-significance cases remain in `test_release_version.py`. |
| Two repeated task wrapper state values | Strict state/action models and the retained completion-field mapping case. |

The current Execution name `command_read` was incorrectly called old in the
previous audit. Its retry-safety parameter is restored alongside compatibility
name `cmd.read`; the test now checks only unknown-outcome safety. Generic
pre-commit rejection/idempotency proof remains with the retry-policy owner.
This restores one case, so 20 removed repetitions produce net -19.

Other count-neutral simplifications remove repeated direct-plus-HTTP checks,
source wording checks, and private connection-count assertions. The task CAS
journey now directly tests an identical property-update retry producing no event;
state/CAS/ownership checks remain in their narrower owners. No formerly separate
parametrized cases were moved into loops to lower collection counts.

Focused runtime checks on the normal Secondary environment: A 77 passed;
B 61 passed; C 69 passed; D 85 passed; read/identity review 70 passed.
The isolated worker environment could collect but stalled on several async
runtime tests; no skips were added, and those modules were rerun normally.
Exact node IDs (including renames), ownership manifests, validation logs and
candidate acceptance evidence are kept in
`/workspace/tmcp-test-reduction-phase2-20261017/`.

The Windows implementer was notified. The integrator re-fetches the remote
canonical branch before merging and publishing; cleanup does not supersede
parallel platform changes. Full candidate acceptance is required before publish.

# Windows platform scope

`platform windows` выбирает native acceptance из `qa/windows`. Параметр отказа
`windows` в проверке неизвестной платформы заменён на `darwin`; позитивная проверка
выбора Windows добавлена в существующую проверку изоляции платформенных слоёв.
Контракты Linux сохраняют существующие selectors.


### Integration with the concurrent Windows branch

Before publication, origin advanced from `4334540` to `e5fd8e4`, adding Windows
execution/deployment and runner fixes. The feature branch merged that canonical
head; the only conflict was this append-only audit document, resolved by
retaining both sections. Windows source, scripts, native QA and new tests remain
byte-identical to the incoming canonical versions.

The updated canonical collection is **2,173** cases; the combined candidate is
**2,151**, preserving all six net new tests from the parallel Windows work.
The cleanup remains net **-22** versus current canonical (including the prior
three-case feature tranche), or **-19** for this follow-up alone. The original
1,486-case acceptance threshold remains **665 cases away**. Native Windows QA
under `qa/windows` remains in its separate platform selection and is not counted
as a Linux full-suite execution. The obsolete `0043a3c` acceptance was cancelled
when the canonical update was discovered; a fresh full gate must cover the exact
merged commit. No stale pre-merge gate is accepted as proof for the merge.
