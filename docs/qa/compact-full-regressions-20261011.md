# Compact full-regression contract alignment

The complete run on candidate `778b2ee51ae277bae4b2b8dc5f4ccf4196592c87`
finished with 2525 passed, eight failed, and two skipped. The command exited 1
on 11 October 2026. These eight failures exercise obsolete public response
shapes; each is mapped to the already-approved compact contract below.
Production Python is unchanged by this follow-up.

## Failure analysis and retained invariants

| Test | Cause | Equivalent retained check |
|---|---|---|
| final_mcp_budget_compacts_large_coordination_metadata | nested coordination/pending view removed | root incoming ID, full sender, bounded text preview and explicit truncation warning; committed command hash/status/exit and body retained; final encoded budget |
| cmd_read_compacts_repeated_long_coordination_inbox_within_wire_budget | two bounded alias arrays replaced by one root incoming preview | exact first four IDs, alias deduplication, eight omitted entries, exact preview lengths, one warning, unchanged command output and text/structured equality |
| cmd_read_preserves_pending_messages_not_emitted_from_messages_cap | public pending alias no longer exists | distinct inbox count includes omitted pending entries once; pending-only source retains all four IDs and ACK/reply requirements; source bodies and delivered state stay unchanged; final encoded budget |
| task_claim_output_accepts_session_lifecycle_and_nullable_checkpoint | public claim/release success is now minimal | actual role tool returns exact `{"ok":true}` for both actions; canonical snapshot accepts missing nullable checkpoint and canonical lifecycle retains 1200 seconds |
| compact_health_matches_closed_role_output_contract | short health omits workflow | exact six-field short response; explicit extended response preserves the complete original workflow, terminal and component diagnostics; both validate |
| schema_release_baseline_is_exact | message input baseline predates chunk-read arguments | exact oracle retained; only executor/coordinator message input hashes and planning byte sizes change from the already-implemented chunk contract |
| extended_role_health_omits_host_privilege_details | default call is short; extended contract now keeps diagnostic fields | short response still omits host details; explicitly extended response preserves terminal diagnostics while excluding auth mode, identity and custom command |
| unhealthy_components_are_a_successful_diagnostic_result | short health no longer publishes status | unsuccessful component health remains a successful diagnostic read with healthy=false; extended component and terminal evidence preserved |

The message input planning snapshot increases from 1510 to 1619 bytes. There is
no tool-count, role, runtime-source, output-schema, or other input-digest change.
Additional short-health cases verify that failed/degraded mandatory components,
terminal failure and workflow failure still affect healthy even when their
individual diagnostics are absent from the short body.

Existing independent compact and chunk fixtures are unchanged. Validation results
and immutable candidate provenance are retained with the handoff. A new full run
and independent exact-SHA acceptance remain required before integration. This
follow-up does not integrate the canonical branch or change a deployed runtime.

## Focused verification

The final focused set passed 221 tests, with two existing standalone-intent
skips and no failures (223 cases, pytest exit 0). It includes all three affected
modules, unchanged independent compact/error/large-message/close fixtures,
message chunks, input budgets and task history/checkpoint/revision/reference
invariants. Production Python and independent fixtures remain unchanged.
This focused result is not a full-regression result.
