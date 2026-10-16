# Atomic test audit: Group C

Base: `df06249d91af79a651a4e113dfb3b65306b1e588`. The assigned manifest lists
90 paths and 769 collected cases at baseline. One duplicate review module was
removed; the 89 remaining assigned paths collect 716 cases (net -53). No test functions
were added. Two current test IDs changed in the transport-ID parameter test and
one event-logging ID changed when its repeated labels were reduced. Exact changes
and owners follow.

## Removed and changed node IDs

| Removed node ID(s) | Current property owner or disposition |
| --- | --- |
| `tests/test_application_boundary.py::test_application_wrapper_is_reused_for_the_same_service` | Application wrapper object identity is an implementation cache detail. Actor immutability and current facade behavior remain owned by `test_application_boundary.py::test_actor_is_immutable_and_copies_mutable_scopes`, `test_application_boundary.py::test_session_gate_enriches_from_authority_without_mutating_input_actor`, and `test_application_boundary.py::test_context_list_projects_the_current_backend_result`. |
| `tests/test_v08.py::test_global_read_uses_batch_queue_lookup` | Removed an internal repository-call optimization assertion. Global-read output and paging remain owned by `test_service.py::test_global_read_defaults_to_latest_500_without_count`; batching is not the external contract. |
| `tests/test_v08.py::test_cancel_after_claim_before_spawn_cannot_resurrect_command` | The minimal execution boundary owner is `test_execution_port.py::test_cancel_between_durable_claim_and_spawn_fences_stdin`; it asserts cancellation prevents the claimed command from reaching execution. |
| `tests/test_v08.py::test_queue_reconciliation_claims_persisted_work_without_submit_signal` | Persisted queued work starting after worker restart is owned by `test_queue.py::test_worker_restart_executes_persisted_command_from_finished_agent`, which creates the durable command before starting the worker. |
| `tests/test_release_version.py::test_installer_packs_from_disposable_source_and_uses_canonical_baseline` | Removed installer-source text checks. Staged installer validation is behaviorally checked by `test_installer_staging_isolation.py::test_staged_validation_isolated_stores_and_cleanup_on_every_exit`; release calculation/stamping are checked by the focused cases retained in `test_release_version.py`. |
| `tests/test_release_version.py::test_consecutive_qa_builds_increment_code_on_same_schema_and_keep_sha` | This chained schema bump, code bump, stamp, no-op rebuild, SHA resolution and schema bump. Its separate properties are owned by `test_code_only_change_increments_third_digit`, `test_manifest_or_database_schema_change_increments_middle_digit`, `test_stamping_keeps_package_semver_and_short_commit_sha`, `test_same_deployed_build_does_not_double_increment`, and `test_mesh_number_storage_migration_increments_schema_version`. |
| `tests/test_release_version.py::test_existing_events_reason_change_classifies_schema` | Event schema significance remains checked by `test_legacy_023_native_events_addition_classifies_024_schema` and `test_events_nonsemantic_regeneration_is_stable_and_properties_remain_significant`; both cover schema-fingerprint changes, including a changed payload property. |
| `tests/test_v010.py::test_output_prune_default_creates_half_capacity_headroom_with_safe_busy_timeout` | Removed private `prune_rows`/connection busy-timeout expectations. Output behavior remains owned by `test_output_retention.py::test_physical_pressure_compacts_legacy_cache_without_logical_prune` and `test_output_retention.py::test_prune_never_scrubs_queued_or_running_command_bodies`. |
| `tests/test_command_replay.py::test_application_never_replays_transport_id[0-run]`, `[1-run]`, `[reused-connector-id-run]`, `[-run]`, and the corresponding four `recovery` IDs | All request-ID values followed the same ignored-ID branch. The two retained representative IDs are `tests/test_command_replay.py::test_application_never_replays_transport_id[run]` and `[recovery]`; run and recovery stay separate because they use distinct request paths. |
| `tests/test_installer.py::test_installer_persists_console_origin_allowlist` | Removed script-shape duplication. Actual ingress default repair and custom-origin preservation remain owned by `test_installer.py::test_ingress_repairs_empty_console_origin_for_existing_install` and `test_installer.py::test_ingress_preserves_custom_origin_and_adds_canonical_origin_once`. |
| `tests/test_installer.py::test_installer_checks_runtime_imports_before_activation` | Staged import and launcher failure behavior is exercised by `test_installer_staging_isolation.py::test_staged_validation_isolated_stores_and_cleanup_on_every_exit`; actual dependency importability is checked by `test_installer.py::test_runtime_dependency_matches_required_mcp_api`. |
| `tests/test_installer.py::test_caddy_example_documents_fleet_ingress_contract` | Retired static example-file wording assertions. The installer doctor’s public ingress success and proxy-404 failure are behaviorally checked by `test_installer.py::test_doctor_requires_public_fleet_ingress` and `test_installer.py::test_doctor_rejects_proxy_404_for_public_fleet_ingress`. |
| `tests/test_installer.py::test_installer_builds_sqlite_before_runtime_import_check`, `tests/test_installer.py::test_update_stages_fixed_sqlite_before_backup_and_activation` | Their ordering checks are represented by the actual isolated validation test `test_installer_staging_isolation.py::test_isolated_validation_still_precedes_backup_and_activation`, which checks SQLite build/validation, then stage/backup/activation ordering. |
| `tests/test_installer.py::test_installer_blocks_fleet_control_schema_downgrade[2-1]` | Same `current > target` guard as retained `[3-2]`; the retained case also checks refusal wording for durable managed topology/trust state. |
| `tests/test_installer.py::test_installer_allows_compatible_fleet_control_schema[3-3]` | Same equality branch as retained `[2-2]`. The distinct forward-compatible case `[2-3]` also remains. |
| `tests/test_installer.py::test_installer_uses_single_generated_systemd_model`, `tests/test_installer.py::test_installer_rotates_releases_and_database_backups` | Removed implementation-string and file-absence checks. Generated split-service caller/security properties remain in `test_execution_topology.py::test_units_restrict_caller_and_preserve_independent_lifetimes`; pruning behavior remains in `test_deployment_retention.py::test_prune_releases_keeps_current_and_previous_usable` and `test_deployment_retention.py::test_prune_sqlite_backups_keeps_latest_two_usable`. |
| `tests/test_installer.py::test_installer_blocks_runtime_schema_rollback_with_legacy_claims` | It hit the same unconditional `current_version > target_version` refusal branch as the retained no-rows case; the installer does not inspect claim rows for this rule. Runtime migration/claim preservation remains separately checked by `test_task_storage.py::test_schema_v18_preserves_pre_cutover_claimed_ready_as_in_progress`. |
| `tests/test_installer.py::test_installer_allows_equal_or_newer_release[0.11.3]` | Same strictly-newer guard branch as retained `[0.12.0]`; equality `[0.11.2]` and a newer version remain as representatives. |
| `tests/test_test_workflow.py::test_shared_unknown_or_deleted_changes_expand_component_feedback[src/terminal_mcp/storage/sqlite.py]`, `[src/terminal_mcp/fleet/protocol.py]`, `[src/terminal_mcp/application/access_mesh.py]`, `[src/terminal_mcp/core/public_errors.py]`, `[scripts/build_compact_catalogs.py]`, `[tests/conftest.py]` | Repeated named paths exercised the same conservative expansion branch. Auth foundation (shared boundary), a new unknown file, and a deleted test remain as three distinct fallback causes; helper transitivity remains owned by `test_test_workflow.py::test_test_helper_changes_select_transitive_consumers` and `test_test_workflow.py::test_affected_selection_normalizes_native_paths_and_helper_consumers`. |
| `tests/test_test_workflow.py::test_source_routes_expose_owner_or_safe_unknown[src/terminal_mcp/storage/work_windows.py-owner-owners1-all]`, `[src/terminal_mcp/application/base.py-unknown-owners3-all]` | Removed a repeated known-owner example and a repeated unknown-source example. The retained owner route is core/read-contract; shared storage boundary behavior is checked by `test_shared_store_changes_keep_direct_feedback_and_expose_all_boundaries`, and generic unknown routing remains in the retained HTTP unknown route case. |
| `tests/test_test_workflow.py::test_invalid_or_narrowed_acceptance_commands_are_blocked[argv0]`, `[argv2]`, `[argv4]`, `[argv8]`, `[argv9]` | Removed extra parser examples for invalid full positional arguments, unsupported `--expected-sha` on `dev`, `component capture`, `platform --changed-from`, and invalid integration component. Remaining nodes still cover invalid full-mode narrowing, invalid component, missing node arguments, invalid timeout, and invalid platform choice. |
| `tests/test_test_workflow.py::test_receipt_binds_exact_clean_full_success[-integration-0-False]`, `[-platform-0-False]` | Both repeated the same `mode != full` evidence eligibility branch as retained `dev`; clean full success, dirty full, and failed full remain separate receipt conditions. |
| `tests/test_execution_topology.py::test_invalid_topology_configuration_is_rejected[executor_rpc_timeout_sec-inf]` | Same non-finite timeout branch as retained NaN; low and high finite boundaries remain. |
| `tests/test_execution_topology.py::test_cutover_refuses_undrained_queue[queued]` | Same non-drained refusal branch as retained active `running`; `unknown` remains to guard fail-closed handling of unrecognized persisted status. |
| `tests/test_cancel_receipt_observed_status.py::test_cancel_wire_preserves_observed_command_status[guard_exit-queued]`, `[guard_exit-running]` | Guard-exit follow-up failures are covered by `test_command_committed_receipts.py::test_cancel_preserves_durable_intent_on_followup_failure[guard_exit-runtime]` and `[guard_exit-lifecycle]`; this wire projection still retains no-failure and enqueue-failure cases for both queued and running statuses. |
| `tests/test_cancel_receipt_provenance.py::test_cancel_retries_preserve_original_execution_provenance[False-queued]`, `[False-running]` | Same-process concurrent cancel retries and one-intent persistence are covered by `test_command_committed_receipts.py::test_concurrent_cancel_retries_keep_one_durable_intent`; queued and running provenance cases remain after repository reopen. |
| `tests/test_live021_attempt_diagnostics.py::test_task_commit_is_readable_after_lost_asgi_body` | Task mutation persistence/reconciliation after lost response is owned by `test_task_create_request_receipt.py::test_lost_response_after_commit_is_reconciled_from_receipt`; command response-loss trace coverage remains in `test_live021_attempt_diagnostics.py::test_lost_response_keeps_command_and_same_rpc_id_runs_independently`. |
| `tests/test_live021_attempt_diagnostics.py::test_attempt_events_survive_minimal_logging[mcp_http_started]`, `[mcp_http_finished]`, `[mcp_tool_started]`, `[mcp_tool_finished]` | Same event-logger minimal-mode behavior for four labels. Replaced with one representative `tests/test_live021_attempt_diagnostics.py::test_tool_attempt_event_survives_minimal_logging`; trace framing and concurrent isolation remain covered by the retained middleware tests. |
| `tests/test_claim_manual_override_review.py::test_manual_state_override_during_lease_survives_cleanup` | Removed a larger duplicate explicit-state/lease-cleanup journey. The simpler equivalent property owner is `tests/test_claim_manual_state_review.py::test_lease_release_preserves_prior_explicit_in_progress`; distinct durable-claim preservation and per-item recovery isolation remain in the other claim review owners. |
| `tests/test_live022_message_diagnostics.py::test_require_reply_promotes_mode_and_ack_does_not_complete_reply[notify]`, `[ack]` | Its message-mode and receipt assertions duplicate `test_access_mesh_messages_v2.py::test_reply_requirement_cannot_be_weakened_by_explicit_notify_mode` and the current sender receipt owners. The run-admission obligation remains in `test_v08.py::test_require_reply_blocks_run_after_ack_until_linked_reply`. |

Two existing node IDs keep their names but were reduced to one remaining property:

- `tests/test_v08.py::test_seen_message_repeats_until_explicit_read_ack` now owns the
  reminder-after-seen timing boundary. The run-block/ACK/release journey is owned by
  `test_agents_native.py::test_direct_message_delivery_blocks_run_until_ack`.
- `tests/test_v08.py::test_queue_affinity_parallelism_and_command_survival_after_finish`
  now owns queue affinity and cross-queue parallelism. Queued/running command survival
  after finish is owned by `test_queue.py::test_queued_command_survives_explicit_agent_finish`
  and `test_queue.py::test_running_command_survives_explicit_agent_finish`.

## Added or renamed IDs

- `tests/test_command_replay.py::test_application_never_replays_transport_id[run]`
- `tests/test_command_replay.py::test_application_never_replays_transport_id[recovery]`
- `tests/test_live021_attempt_diagnostics.py::test_tool_attempt_event_survives_minimal_logging`
- `tests/test_cancel_receipt_provenance.py::test_cancel_retries_preserve_original_execution_provenance[queued]`
- `tests/test_cancel_receipt_provenance.py::test_cancel_retries_preserve_original_execution_provenance[running]`

These are representative IDs for existing behavior, not new behavioral claims.
There are no new functions or new properties.

## Review boundaries

The task/review workflows in `test_v010_workflow_model.py` were checked against
current task storage, atomic ownership, refs and coordination owners; their parent
candidate binding/rejection, review completion, relation history and archive
migration cases remain distinct. `test_v08.py` was checked against local receipt,
message diagnostics, queue and expiry owners. Its supported local messaging,
message-required reply gate, alert gate, queue-affinity/parallelism, schema migration
and observer projection remain. Queue restart reconciliation is owned by
`test_queue.py::test_worker_restart_executes_persisted_command_from_finished_agent`.
No retired cross-node message transport was used to justify a removal.

The deployment workflow, remaining installer tests and topology tests retain their
distinct rollback, data preservation, approval integrity, process, auth, permission,
fencing, cutover, native-runtime and managed-service safety boundaries. The broad
owned audit is incomplete outside the modules listed above; do not treat this as a
claim that every remaining composite function has been atomized. No runtime code,
shared fixture, test-selection rule or test-plan registry changed.
