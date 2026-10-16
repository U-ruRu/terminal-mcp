# B1 atomic test audit

This audit covers only paths in `test-group-B1.json`. It removes compound
scenarios whose current properties have smaller owners, retires assertions
against the old Access Mesh message transport, and reduces repeated values that
exercise one validation or normalization branch. Current authorization,
managed-session, persistent-data, lifecycle-event, and local-message contracts
remain in scope.

## Counts

- Original B1 manifest: 82 modules, 605 collected cases (parameters included).
- Current B1-owned selection: 555 cases; net change: -50, with no new cases.
- Original full baseline: 2,973 cases. Current full collect-only selection is
  2,497 cases, including the 22 idle-maintenance performance cases. This is a
  collection count, not a runtime test result.
- B1 target: at most 332. Current selection is 223 over that target. The
  remaining larger scenarios listed below preserve distinct current boundaries;
  no further deletion was justified by an equivalent simple owner during this
  pass.

## Removed and reduced checks

| Removed node or family | Retained property owner / reason |
| --- | --- |
| `test_access_mesh_agent_observe_fleet::test_agent_observe_preserves_public_fields_but_is_explicitly_local` (deleted module) | `test_access_mesh_session_number_public::test_observe_only_named_attached_agents_and_four_fields` now also fails if observation calls the live locations endpoint. The current rule is local attached-agent projection. |
| `test_access_mesh_alert_ack_contract::test_expired_session_emits_single_message_alert_and_ack_removes_block` (deleted module; known baseline failure) | Split deterministic owners: `test_qa_expiry_alert_gate::test_expiry_alert_ack_clears_legacy_obligation_for_same_agent` and `test_expiry_alert_retention_health::test_expiry_alert_ttl_boundary_keeps_original_wire_and_ack`. The old full-app clock choreography returned `access_mesh_cleanup_pending` after advancing time and conflated expiry, alert creation, cleanup, and ACK. |
| `test_access_mesh_expired_number_reuse_qa::test_expired_number_reusable_without_erasing_historical_identity` | `test_access_mesh_session_numbers::test_reserve_conflict_proposal_expiry_and_release` owns reservation expiry/release; `::test_recycled_number_forms_new_identity_epoch_and_retains_old_history` owns old/new registry identity. The remaining SQLite-level reissue test stays because it exercises live-slot uniqueness indexes through `AccessMeshStore.apply_event`. |
| `test_access_mesh_mcp_runtime::test_message_projection_does_not_hide_queued_remote_delivery` | Removed transport-only pending-peer projection. The current advertised output remains checked by `::test_access_outputs_validate_as_advertised`; remote delivery is retired. |
| `test_access_mesh_session_numbers::test_http_number_replication_converges_future_identity` | `test_access_mesh_new_node_catchup::test_new_node_catches_collisions_reservations_and_slots_without_rewriting_history`, `test_access_mesh_replication_http::test_http_two_issuers_and_offline_hot_path`, and `test_access_mesh_session_numbers::test_collision_winner_history_and_max_deadline` own snapshot convergence, HTTP negotiation, and canonical winner/history respectively. The deleted case repeated the same collision and attach path as one large journey. |
| `test_access_mesh_message_application::test_local_send_commits_without_requesting_or_scheduling_peers` assertion that `accept(..., peers=[])` was passed | Retained the actual application rule: local sends do not call or schedule peers. The storage `peers` argument is retired; checking the old empty-list plumbing was implementation-only. |
| `test_access_mesh_messages_v2::test_directed_agent_attached_on_both_nodes_gets_each_local_inbox` (two directional cases) | `test_local_messages_24h_victor::test_same_agent_attached_on_two_nodes_has_independent_local_histories` and `test_access_mesh_message_application::test_local_send_commits_without_requesting_or_scheduling_peers` retain local delivery/history and no-network behavior. The former two-node exchange repeated the retired remote-recipient setup. |
| `test_access_mesh_messages_v2::test_attribution_receipts_and_peer_auth_reject_forgery` | Retired remote delivery/receipt behavior. The retained authenticated endpoint guard is `::test_http_router_checks_peer_auth_and_request_size`, now expecting `feature_disabled` and `outcome=not_committed`; local sender attribution stays covered by application/storage tests. |
| `test_application_fleet_boundaries::test_replication_and_control_require_verified_mesh_actor` role variants `operator`, `legacy` | Kept one representative non-mesh role. The same function still checks both independent application call sites (`read_identities` and `internal_state`) and that neither reaches its port. |
| `test_application_operator_mesh::test_mesh_role_and_verified_peer_are_required` roles `legacy`, `coordinator`, `operator` | Kept `executor` as a representative rejected role while preserving both `access_resolve` and `route` call-site assertions. The shared actor-role guard takes the same branch for all non-mesh roles. |
| `test_application_operator_mesh::test_http_mesh_maps_only_transport_status_and_preserves_domain_error[message_not_found-404]` | Kept the same 404 mapping through `slot_not_found`; both codes are in the same explicit status set in `src/terminal_mcp/application/mesh.py`. The 403 and 409 domain mappings remain separately covered. |
| `test_expiry_alert_retention_health::test_expiry_alert_ttl_boundary_keeps_original_wire_and_ack` cases `[0-True]`, `[1-False]`, `[1-True]` | Kept `[-1-False]` (one instant before TTL), `[0-False]` (exact TTL boundary), and `[-1-True]` (an acknowledged alert before TTL). Those cover the live boundary and ACK branch; later expired values take the same expired branch. |
| `test_fleet_control_writer_trace::test_repository_has_no_raw_fleet_control_wal_to_main_copy_path` | Removed a source-text grep. `test_fleet_control_corruption_repro::test_wal_concurrency_never_replaces_fleet_control_main_header` exercises the observable integrity property under concurrent WAL activity. |
| `test_fleet_identity::test_identity_revision_must_be_positive[-1]` | Kept revision `0`, the boundary for the `revision <= 0` validation branch. |
| `test_fleet_route_first_contact::test_first_contact_unreachable_authority_does_not_invent_route[404]` | Kept 401 and 500 representatives. All non-success statuses take the same unavailable-authority path and leave route storage empty. |
| `test_local_messages_24h_victor::test_local_cycle_ack_replies_and_remote_recipient_exclusion` | Its local send/inbox/ACK/reply/history properties are owned by `::test_all_local_modes_support_inbox_idempotent_ack_reply_and_history`; remote-only recipient rejection remains in `test_access_mesh_messages_v2::test_remote_only_recipient_is_rejected_before_local_mutation`. The old case combined those properties with a two-node fixture. |
| `test_local_messages_24h_victor::test_cutover_pending_jobs_expiry_and_restart` | Its direct `queue`/`pending` calls target retired storage methods. The required raw-old-row/no-false-ACK/restart boundary remains `::test_old_invalid_message_is_ignored_and_outbox_finalizes_without_ack`; local TTL and pruning remain in `::test_ttl_boundary_ack_does_not_extend_reply_has_own_ttl_and_restart_prunes`. |
| `test_local_messages_24h_victor::test_storage_rejects_new_peer_delivery_jobs_before_commit` | Removed with the retired `accept(..., peers=...)` API. The current public `scope="fleet"` fail-closed behavior is owned by `test_access_mesh_message_application::test_fleet_scope_is_rejected_before_authorization_or_storage`. |
| `test_local_messages_24h_victor::test_legacy_peer_message_request_is_terminally_rejected_without_queue` queue-count assertions | Kept the test and its authenticated endpoint rejection, but replaced the retired outbox-table inspection with before/after `local_cutover_report` equality. It also retains the read-only live agent-presence route assertion. |
| `tests/test_mcp_events.py::test_attach_only_after_local_row_commit_and_no_duplicate` | `test_mcp_events_managed_storage::test_explicit_local_attach_callback_creates_one_event_without_neighbor_presence_rows` checks one local attach event and no neighboring presence rows. |
| `tests/test_mcp_events.py::test_remote_grant_is_not_local_attach`, `::test_persistent_presence_for_other_node_not_misattributed`, `::test_persistent_remote_presence_grant_not_attached_to_local` | `test_mcp_events_managed_storage::test_foreign_work_session_and_grant_not_a_local_attach` owns the foreign session/grant boundary; `::test_foreign_origin_explicit_attach_on_this_node_only` owns node-local explicit attachment. |
| `tests/test_mcp_events.py::test_observer_not_touch_target_or_rearm_idle` | `test_mcp_events_managed_storage::test_observer_is_read_only_for_managed_idle_clock` checks no activity mutation; `::test_managed_inactivity_strict_180_once_per_real_activity_episode` owns the idle boundary and rearm. |
| `tests/test_mcp_events.py::test_standard_webhooks_signature_uses_exact_body` | `::test_webhook_signature_and_stable_event_id` checks the signed callback body through the actual event adapter. |
| `tests/test_mcp_events.py::test_real_persistent_presence_and_session_expiry_per_local_node`, `::test_real_limited_work_session_explicit_end_delivers_once`, `::test_persistent_attachment_stays_untouched_by_read_and_idles`, `::test_persistent_remote_presence_grant_not_attached_to_local` | Current minimal owners are the managed-storage tests named above: `::test_managed_end_without_presence_once_even_after_metadata_retry`, `::test_managed_inactivity_strict_180_once_per_real_activity_episode`, `::test_observer_is_read_only_for_managed_idle_clock`, `::test_foreign_work_session_and_grant_not_a_local_attach`, and `::test_foreign_origin_explicit_attach_on_this_node_only`. Legacy AgentStore event/expiry tests remain. |
| `tests/test_mcp_events_fleet_lifecycle.py::test_generated_mesh_pages_and_chunk_contracts` | Removed generated-schema example assertions duplicated by `::test_legal_unicode_event_canonical_frames_and_chunk_restart` and `::test_complete_recipient_pages_over128_restart_and_withdrawal`; `::test_generated_mesh_wire_catalog_is_current` remains the generated artifact check. |
| `tests/test_mcp_events_session_number.py::test_number_catalog_and_other_schemas_unchanged` | The current artifact/native owner is `::test_generated_events_catalog_matches_native_runtime`; behavior and schema examples remain in event subscription tests. |
| `tests/test_mcp_events_session_number.py::test_invalid_number_rejected_before_challenge[1234\\n]` | Removed the second padded-string value; `" 1234"` remains for the same exact-four-ASCII-digit validator rejection class. Null, integer, length, Unicode-digit, and non-digit classes remain. |
| `tests/test_mesh_human_names.py::test_established_alphabet_and_cycle_with_leading_zeros[0123-Tango-5]` | Kept `0000`, `0025`, `0026`, and `9999` for the start, alphabet edge, cycle boundary, and high supported edge. `0123` exercised an interior mapping. |
| `tests/test_mesh_session_authority_review.py::test_session_control_rejects_stale_or_malformed_authority_epoch` values `3` and `5` for both `revoke` and `drain` | Kept stale integer boundary `0`, boolean-as-integer `True`, and wrong-type `"4"` for each route. Positive stale integers share the same stale-epoch branch. `::test_current_authority_epoch_is_accepted` remains. |
| `tests/test_mesh_vpn.py::test_ipv6_endpoint_rejects_non_global_ambiguous_or_dns_address[[2a04:5200:fff5::344a]:61063]` | Removed a lowercase spelling of a valid global IPv6 address from the rejection table; `::test_enrollment_accepts_numeric_ipv4_and_global_ipv6_underlay` retains IPv6 canonicalization using uppercase input. All invalid address and port classes remain. |
| `tests/test_mcp_events_end_reason.py::test_other_committed_causes_are_not_mislabeled` aliases `SessionEnded`, `session_end`, `operator_end`, `hard_duration`, `SlotDeleted`, `replicated_cycle_fence`, `snapshot_catchup`, `session_interrupt` | Retained one value from each normalization branch (`manual`, `expired`, unchanged `SlotSuspended`) plus the null fallback. The removed values occupy the same set-membership normalization branches in `src/terminal_mcp/core/session_end_reasons.py`; manual/expiry actual-transition tests remain. |

The two removed module files were fully removed from the owned path set. Their
names can be removed from the shared selection registry by the coordinator.

## Composite functions reviewed and retained

These are longer than the preferred atomic shape. I retained them because the
exact current boundary has no smaller current owner; deleting them would remove
unique behavior. They are listed for follow-up decomposition only if a future
authorized task establishes smaller owners:

- `test_access_mesh_identity_history_qa::test_collision_preserves_task_claim_comment_message_and_command_history`: preserves pre-merge claim, comment, local message, and command attribution while future identity changes. No other current collision owner checks all of those historical authorship rows.
- `test_access_mesh_new_node_catchup::test_new_node_catches_collisions_reservations_and_slots_without_rewriting_history`: new-node snapshot/catch-up boundary across reservations, collisions, and slot history; existing snapshot gap tests do not exercise that new-node state.
- `test_fleet_v1_authority::test_request_dedup_and_message_gate_share_one_home_obligation`: distinct persistent permit/idempotency/message-obligation gate ordering; retained atomic permit and gate owners are separate.
- The long managed trust/topology mutation and forwarding cases in `test_fleet_v1_authority`, plus persistent authority/drain cases in `test_persistent_fleet`, each exercise current authenticated cross-node commit/fencing boundaries with no equivalent simple app owner.
- `test_mcp_events_session_number::test_shared_number_fanout_exact_recipient_refresh_and_selective_unsubscribe` and expiry/reuse case: current HTTP/MCP adapter, callback owner, number binding, and no-retarget boundaries; component subscription owners do not test this adapter wiring.
- Recipient-hold, message-event propagation, and event restart cases across `test_mcp_events_fleet_lifecycle`, `test_mcp_events_global_held_recipients`, and `test_mcp_events_recipient_holds`: retained because MCP `message.received` propagation and recipient holds remain supported, separate from retired inbox transport.
- `test_mesh_vpn::test_mobile_operator_api_enforces_managed_peer_and_pinned_key`, plus VPN cutover/reconcile cases: retain current operator-auth, pinned-key, route, and health boundaries.

## Verification scope

No runtime pytest bodies were run in this sandbox after the runtime-wait
instruction. Coordinator verification is required for changed async, HTTP,
SQLite, and native-sensitive paths. Exact changed test modules are listed in the
B1 progress/final handoff; collection-only, Ruff, and compilation are recorded
there separately from behavior results.
