# Compact full-regression contract synchronization

This follow-up retains the production Python implementation from
30eeb9d5d9f82d9a09ccaf825daac77088843332. It changes only test expectations and
reviewed schema snapshots. The 160-case independent compact acceptance remains
frozen; every added case is still collected.

The first full regression on 30eeb9d was cancelled by native session expiry:
command a0f1aa25, 2026-10-11T17:42:56.243Z, native exit -15. The log stopped near
48 percent, with nine failures. No pytest exit or JUnit was emitted. This is an
INCOMPLETE run, not a full PASS. No overlapping replacement run was started.

## Reproduced failures and preserved checks

| Test | Confirmed cause | Equivalent new-contract assertion |
|---|---|---|
| issuer_replay_sealed_receipt_and_attached_shell_reads | removed command_run.command wrapper | root cmd_hash; no inline output; bounded polling only via command_read; output/journal/end checks retained |
| role_messages_share_durable_obligations_across_metadata | removed health.status | ok and healthy are true; status absent; message ACK/reply checks unchanged |
| executor_checkpoint_shares_coordinator_history_and_preserves_state | removed task_claim response task | exact minimal release success, separate public task_get state/revision compared with stored task; checkpoint unchanged |
| three_effective_role_contracts_match_reviewed_manifest | stale effective schema snapshot | regenerate with mesh_schema_manifest on isolated create_app; all tool counts, input/output budgets and annotations still asserted |
| manual_start_resumes_original_window_then_allocates_next_window | removed root required_action/message_hash aliases | system message requirements=ack and explicit ACK using messages[0].id; same deadline/window checks |
| ack_gate_blocks_only_run_and_surfaces_messages_on_identity_read | removed details.pending_messages | root message ID and requirements=ack, original coordination error code and refusal unchanged |
| alert_gate_blocks_work_but_never_blocks_cancel | removed details and required_action | root message ID and requirements=reply, original coordination error code and actual cancel refusal unchanged |
| architecture_a_preserves_exact_published_mcp_contracts | stale legacy catalog digests | same builder/serialization hash oracle, regenerated snapshot; forbidden input identity fields still rejected |
| mcp_code_free_read_paths_use_canonical_tools | legacy raw-text read aliases replaced by command record | command hash/status/exit code preserved; AsyncMock verifies actual anonymous read argument rather than removed display label |

The first eight reproduce together as 8 FAIL (669c4886); the ninth reproduces
separately (dc586ce2). After synchronization: all nine PASS (4769c336). All six
containing test modules: 28 PASS, exit0 (ddcde237). These overlap and are not summed.

No command/gate/session/ACK implementation was modified. Existing minimum/maximum
schema budgets remain in force. Native naming, current-canonical intent and Events
integration are separate gates; this commit does not merge or deploy those lines.

A new immutable SHA needs independent diff and exact-SHA acceptance before the
single replacement full suite. Preserve the cancelled original receipt and guard.
