# B2 atomic test audit

## Scope and result

This audit changes only the B2-owned tests. The exact ownership manifest is
`/workspace/tmcp-idle-kilo18-evidence-20261016/test-group-B2.json`; all 50 listed
paths were read and reviewed. Current collection is **425 cases**, down from the
manifest's **505**, for **80 removed, 0 added, net -80**. The requested local budget
is 277; this pass is 150 cases above it. I stopped reductions where the remaining
case protects a distinct current authorization, identity migration, durable-data,
lease/fencing, managed-slot, or local messaging rule. No new cases were added.

## Removed cases and current owners

- `tests/test_access_slot_inheritance_http.py`: removed
  `test_standalone_defaults_duration_updates_inherited_slot_after_session_start`,
  `test_standalone_defaults_edit_does_not_restart_active_session`, and
  `test_standalone_slot_override_reset_uses_current_defaults`. These repeated the
  policy snapshot/default calculations owned by
  `tests/test_work_window_store.py::test_policy_is_per_slot_and_existing_window_is_snapshot`
  and `::test_legacy_adoption_keeps_source_policy_for_next_windows`; actual HTTP
  policy application and reset remain in
  `tests/test_access_slot_inheritance_http.py::test_real_fleet_http_policy_reaches_native_slots_and_reset`.
- `tests/test_android_slot_http_window_qa.py`: removed its two collected cases,
  `test_http_next_policy_does_not_change_active_window_deadline` and
  `test_http_slot_override_keeps_other_slot_and_issuer_defaults_unchanged`. The
  first invariant remains at the access policy HTTP/restart boundary in
  `tests/test_android_slot_policy_continuation.py::test_http_future_policy_preserves_active_thresholds_and_survives_restart`;
  override/reset behavior remains in
  `tests/test_access_slot_inheritance_http.py::test_real_fleet_http_policy_reaches_native_slots_and_reset`.
  The file remains as a helper module imported by owned tests, and collects zero
  tests.
- `tests/test_auth_path_policy.py`: trimmed the `_interface` route matrix from six
  entries to three (removed `/mcp/` and the two trailing-slash role paths), keeping
  `/mcp`, executor, coordinator and the neighbor rejection. All entries hit the
  same `MCP_PREFIXES` prefix branch in `AuthMiddleware._interface`. Trimmed the
  `_scopes` matrix from eight routes to `/actions/run`; source returns the same
  fixed read-only scope for every path/method, and bearer route behavior remains
  covered by `tests/test_http_auth.py`. Paired access handoff and fleet enrollment
  cases remain because `_paired_console_scopes` has distinct route/method branches.
- `tests/test_agent_identity_values.py`: removed
  `test_generated_suffix_uses_eight_crockford_symbols`; its length/alphabet
  invariant is already checked through the current public generator by
  `test_generated_agent_id_has_current_call_sign_and_suffix`, which also checks
  the call sign and complete format.
- `tests/test_provider_identity_domain.py`: reduced repeated cases within existing
  parameter families. Identity non-aliasing retains different subject, conversation,
  provider, and field-boundary examples; provider allowlist retains empty and forged
  namespace inputs; missing metadata retains empty, partial, and public-field-only
  inputs; identifier coercion retains one wrong-type value for each required field;
  invalid identifiers retain over-limit, control-character, and invalid-Unicode
  representatives; Codex thread validation retains missing and wrong-type cases.
  Removed rows were repeated examples of those same `_identifier`, provider
  allowlist, no-fallback, and identity-key branches (including additional control
  characters, not distinct control handling). Stable hints, privacy, field
  precedence, and principal scoping remain tested.
- `tests/test_mcp_provider_evidence.py`: reduced
  `test_codex_thread_identity_without_required_root_or_call` from three metadata
  shapes to the minimal `threadId` input. The selected MCP metadata extraction
  result is unchanged by optional root/call/progress fields; this is checked at the
  identity owner by `tests/test_provider_identity_domain.py::test_codex_identity_is_principal_scoped_and_does_not_require_session_or_call`.
  MCP extraction still has its own current caller test.
- `tests/test_mcp_observe_nullable_session_metadata.py`: removed the non-null
  persistent metadata parameter row; current non-null persistent projection remains
  in `tests/test_mcp_output_contracts.py::test_observe_variants`. Nullable and
  missing transitional fields remain in this file.
- `tests/test_managed_identity_authority.py`: removed the second unrelated-resource
  value from `test_authorizer_does_not_broaden_principal_grants`. Both resource
  values took the same `access_denied` mismatch branch; the remaining resource
  mismatch and independent client mismatch cover the two distinct grant dimensions.
- `tests/test_managed_session_application.py`: removed repeated application-level
  examples whose current state rule has a direct owner: warning/draining admission
  (`test_draining_read_finalization_and_new_work_branches` and
  `test_expiry_admits_only_end_and_status` in `tests/test_managed_session_policy.py`),
  end/start budget and policy extension (`test_end_start_keeps_deadline_but_advances_session_and_epoch`
  and `test_extend_recomputes_draining_to_active_without_session_restart` in
  `tests/test_work_window_store.py`), principal mismatch
  (`test_other_principal_cannot_reuse_active_session` plus grant checks in
  `tests/test_managed_identity_authority.py`), operator grant
  (`test_operator_operation_requires_operator_grant`), expired-window recovery
  (`tests/test_window_recovery.py::test_restart_after_long_offline_preserves_deadline_and_cooldown`),
  and legacy adoption (`tests/test_work_window_store.py::test_legacy_session_adoption_preserves_exact_ids_deadline_and_provenance`).
  Read-only admission, stale-session end fencing, and missing-port/timeout constructor
  validation remain because their managed-application boundaries have no same-component
  owner. The authority recheck parameter matrix now keeps the three distinct stored-state
  branches once each rather than repeating each with resolved and unresolved actor
  inputs. Stale authority fencing keeps one representative new-work operation;
  operation-family classification remains in `tests/test_managed_session_policy.py`.
  Expiry commit/fence ordering, retry/cancellation, real queued-command fencing,
  deadline consistency, operator paths, live legacy adoption, event admission, and
  notification failure remain in this application owner.
- `tests/test_identity_migration.py`: removed
  `test_b3_loss_of_auth_after_clean_is_not_mistaken_for_first_install` because
  `test_inspect_reports_protected_auth_loss_after_clean` already performs the same
  clean, protected-auth deletion, missing-protected-auth status, and fail-closed
  identity check. Removed the `foundation-only` replacement row from
  `test_b3_replacement_auth_blocks_startup_and_clean`; empty and initialized-but-
  unmarked stores exercise the two meaningful schema classes for the same missing
  lineage marker guard. The generic foreign complete-auth replacement scenario was restored after review
  found its trusted current-auth checkpoint recovery and post-restore refresh-token
  guarantee were not owned by the same-key rejection case. The distinct same-key
  foreign-auth rejection remains in
  `test_b3_foreign_complete_auth_using_same_verifier_key_rejected`; verifier-key
  mismatch and recovery remain in
  `test_b3_swapped_verifier_key_is_detected_and_original_key_recovers`. Forward and reverse deleted-tail audit sequence tests and
  the all-audit-rows-deleted high-water boundary are retained. All migration, auth
  anchor, current credential, and durable data cases remain represented.
- `tests/test_window_recovery.py`: removed negative batch-limit and negative lease
  duration values. Zero/lower bound, upper bound, bool, float, and string cases
  remain; negative integers hit the same lower-bound rejection branch as zero.
- `tests/test_work_window_domain.py`: removed interior phase samples at opening,
  inside warning, and after expiry; the retained set still checks both sides and the
  exact warning, draining, and expiry boundaries. Removed two delta examples that
  repeated the same active/warning results; active, warning, draining, and expired
  recalculation remain. Removed bool/float mutation rows because the same strict
  integer helper is directly checked by `test_policy_rejects_coercion`; the other
  mutation error codes and no-mutation guarantee remain. Coercion examples are one
  `True` representative per field because these fields share `_integer` validation.
- `tests/test_work_window_store.py`: removed negative expected revision; zero and
  bool/float/string classes remain. Negative and zero revisions use the same
  `revision_invalid` lower-bound branch.

## Reused, changed, and new tests

- **Reused:** current store/domain owners listed above, `test_real_fleet_http_policy_reaches_native_slots_and_reset`, current bearer HTTP auth cases, current provider identity/caller cases, and B3 auth-integrity owners.
- **Changed:** existing parameter tables in `test_auth_path_policy.py`,
  `test_provider_identity_domain.py`, `test_mcp_provider_evidence.py`,
  `test_mcp_observe_nullable_session_metadata.py`,
  `test_managed_identity_authority.py`, `test_managed_session_application.py`,
  `test_identity_migration.py`, `test_window_recovery.py`,
  `test_work_window_domain.py`, and `test_work_window_store.py`.
- **New:** none.
- **Removed:** the cases and parameter rows mapped above. No skips, xfails,
  deselection, renaming, or collection overrides were used.

## Remaining composite checks and boundaries

Reviewed remaining long scenarios in migration, managed-session cleanup, pairing
exchange, public Android slot paths, intent freshness, restart/recovery, and local
agent messaging. They combine a real current caller with a distinct commit,
restart, authorization, rollback, or public-shape boundary; removing them would
leave that boundary without a current owner. The audit does not change the supported
`/mcp` route, legacy/persistent slot shapes, local inbox/ACK behavior, recipient
holds, remote claim lease fencing, or durable migration/restart guarantees. Cross-group
owner references point to nodeIDs present in this checkpoint; coordinator should
reconfirm them after the group patches are integrated.

Additional reductions after the initial checkpoint

- `tests/test_android_slot_public_paths.py`: removed
  `test_public_attach_and_operator_read_observe_the_same_active_window` because the
  actual operator HTTP projection and unchanged-current-window mapping remains in
  `tests/test_access_slot_inheritance_http.py::test_real_fleet_http_policy_reaches_native_slots_and_reset`;
  public MCP attach after code rotation remains in
  `::test_rotated_operator_code_connects_through_public_mcp`. Removed
  `test_explicit_deadline_for_publicly_created_slot_preserves_start`, whose start-time
  budget rule is owned by `tests/test_work_window_domain.py::test_total_duration_is_from_opening_not_from_mutation_time`
  and whose HTTP deadline route remains in
  `tests/test_access_mesh_operator_http.py::test_deadline_override_and_nonrearm_end_cooldown`.
  Removed `test_operator_end_fences_the_actual_number_window`, covered by
  `tests/test_access_mesh_session_numbers.py::test_mesh_end_fences_all_attached_nodes_and_ignores_stale_cycle`
  and the retained operator HTTP end check. Removed
  `test_rotation_retry_preserves_window_binding_and_stable_recipient_identity`;
  replay/generation/restart remain in
  `tests/test_android_slot_code_generation.py::test_rotation_generation_is_atomic_replayable_and_survives_restart`,
  and replication preserves the binding in
  `tests/test_access_mesh_native_lifecycle.py::test_snapshot_rotation_preserves_session_but_missed_stop_fences`.
  The would-be shortened-merged-deadline rejection was restored: no other current
  test owns this exact operator request boundary.
- `tests/test_android_slot_policy_continuation.py`: removed
  `test_http_future_policy_preserves_active_thresholds_and_survives_restart` and
  `test_explicit_window_extension_and_retry_leave_future_defaults_intact`. Future
  policy/current-window snapshot behavior remains in
  `tests/test_access_mesh_future_policy.py::test_three_hours_applies_to_next_window_not_current`
  and `::test_explicit_window_action_changes_deadline_but_keeps_current_thresholds`;
  durable snapshot/restart is checked by
  `tests/test_work_window_store.py::test_snapshot_survives_restart_with_immutable_contract`.
  Retained the narrow stale-policy-revision HTTP rejection case as the public adapter
  boundary; storage CAS remains independently owned by
  `tests/test_work_window_store.py::test_window_mutations_are_atomic_cas_across_store_instances`.
- `tests/test_android_slot_code_generation.py`: removed
  `test_create_start_and_policy_edits_keep_same_code_generation`; the policy/code
  generation invariant has a direct current owner at
  `tests/test_access_mesh_future_policy.py::test_code_generation_does_not_follow_policy_revision`.
  Retained the public rotation receipt/replay/restart case.
- `tests/test_work_window_store.py`: removed
  `test_extend_recomputes_draining_to_active_without_session_restart`; the domain
  phase transition is directly checked by
  `tests/test_work_window_domain.py::test_signed_delta_recomputes_phase_without_sticky_warning`
  (the draining-to-active parameter), while durable CAS/revocation remain separate
  store tests.
- `tests/test_managed_session_application.py`: removed
  `test_repeated_expired_gate_calls_keep_stable_error_after_revocation` because the
  current first expiry/fence owner is
  `::test_expiry_fences_before_returning_session_expired`; removed the compound
  operator status + policy + window mutation projection because the component
  behavior is owned by work-window domain/store tests and operator grant checks by
  `tests/test_managed_identity_authority.py::test_operator_operation_requires_operator_grant`.
  Application cases initially treated as repeats were restored when their current
  boundary had no exact owner: `test_read_only_admission_cannot_start_or_operator_mutate`,
  `test_normal_end_start_does_not_buy_a_new_budget`,
  `test_missing_security_ports_and_unbounded_timeouts_are_configuration_errors`,
  `test_operator_requires_both_endpoint_role_and_independent_grant`,
  `test_gate_fences_authorizer_epoch_that_does_not_match_bound_session`,
  `test_draining_allows_idempotent_active_start_but_blocks_new_session_after_end`,
  and `test_legacy_adoption_in_draining_preserves_existing_session_instead_of_starting_new`.
  They retain managed-application scope admission, stale-session fencing, constructor
  security-port/timeout validation, endpoint-role/grant dual authorization, authorizer
  epoch consistency, draining admission, and adoption of a current legacy session
  while draining. These rules are separate from domain phase and grant-owner tests.

The 277-case target is not met: **148 cases remain above budget** after reviewing all
50 assigned modules. No case was removed solely to meet the count. The remaining
long scenarios principally cover distinct durable migration/data preservation,
managed authorization/fencing, recovery leases, current provider identity,
replication, local message receipt/ACK, and event recipient-hold obligations. The
shortened merged-deadline rejection, trusted identity checkpoint recovery, and the seven restored managed-application cases
are flagged as unique current properties and retained. Full suite acceptance and
all modified async, HTTP, and SQLite-backed bodies are deferred to the coordinator's
authorized execution environment; this sandbox's earlier async/HTTP/SQLite waits
are recorded as environment stalls, not test outcomes.
