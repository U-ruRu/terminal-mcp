# TMCP-05 / RUN-01 — QA remediation HANDOFF (Delta-3)

**Status: verified offline, ready for independent coordinator acceptance. NOT DEPLOYED. ChatGPT live A/B NOT TESTED.**

Executor: Delta-3 / Secondary / @Solvenger legacy, session_ref ws_K8FqEHCj5aC8cReA29GuVQR8, expires 2026-10-10 23:20:07 UTC. Coordinator Zulu-2 (suffix dot, expires 23:10:19 UTC) confirmed exclusive generator scope.

**Base:** 2649bc445252b8af9d34b6eee1dec2ce1ea6a601; independent QA Bravo-3: 36914f1fd19518ea0c40afb5eceadca0086230ca. Worktree /workspace/terminal-mcp-tmcp05-delta3-20261010; branch task/TMCP-05-RUN-01-delta3-20261010. QA original was preserved byte-for-byte at docs/compact-contracts/qa-tmcp01-bravo3-run01 and remains untouched in its own remote branch.

## Change scope

- src/terminal_mcp/mcp/compact_annotations.json: source-anchored runtime rules for B3/B4/B5/B6/B8 and G1/G2.
- src/terminal_mcp/mcp/compact_contracts.py: optional action-scoped runtime rules are validated against the canonical action registry, rendered with action labels, and included in coverage provenance.
- docs/compact-contracts/generated: 10 files deterministically rebuilt; only catalog-executor.json, catalog-coordinator.json, coverage.json, metrics.json, provenance.json changed.
- tests/test_compact_tmcp05_completion.py: nine additional tests including runtime behavior on private temporary SQLite DBs.
- docs/compact-contracts/qa-tmcp01-bravo3-run01: original independent QA scripts, reports, tests and evidence preserved without modifications. Adapted scripts were placed only in a separate server artifact directory.

## Individual findings — code evidence → generated rule → runtime proof

| ID | Runtime authority and exact issue | Description correction and QA evidence |
|---|---|---|
| **B3** | core/tasks.py, _action_archive: raw archive_note takes precedence over note before _clean_reason strips. archive_note consisting of spaces masks a valid note and fails. Canonical model requires only one non-null alternative. | Describes canonical alternative, meaningful non-whitespace requirement and dual-field mask. Rerun whitespace-regressions.json includes archive_note_whitespace and archive_note_whitespace_masks_valid_note; new runtime pytest confirms meaningful fallback when archive_note absent. |
| **B4** | core/tasks.py, _action_release: release_reason optional even with live claim; omitted reason falls back to released_by_owner. Supplied whitespace-only reason fails after strip; idempotent unclaimed path returns before parsing reason. | Description distinguishes optionality from nonblank supplied value. Whitespace QA and new runtime pytest verify rejection and successful omission. |
| **B5** | core/tasks.py, _action_state and storage/tasks.py, set_workflow_state: any admitted agent may change state despite other live claimant. expected_revision does not guard content CAS, no content revision increment or claim release. | Executor task_state and coordinator task_manage/state mention these effects. Independent evidence records revision 1→1; new runtime pytest exercises deliberately stale expected_revision and retained claim. |
| **B6** | core/tasks.py, _action_claim + _clean_reason(max_length=160): canonical-valid whitespace claim_intent rejected after trimming. | Action-scoped claim description explicitly requires non-whitespace intent. Rerun whitespace QA plus runtime pytest confirm. |
| **B8** | core/tasks.py, _action_comment: whitespace-only comment_text rejected; canonical Pydantic length permits it. Checkpoint action forbids expected_revision. | Coordinator and executor descriptions both express nonblank text; checkpoint exclusions remain. QA whitespace regression and new pytest confirm. |
| **G1** | core/tasks.py, SAFE_PARTICIPANT_FIELDS, _owner_error, _update_from_kwargs, _action_archive, _action_relate, _action_unrelate. With a live claim, only owner may archive/relate/unrelate or perform workflow-changing update. Nonowner may safely update title, description, next_action, tags. update with non-null state/checkpoint is rejected, use dedicated actions. | Generated task_manage explicitly covers owner_required, safe participant fields, no-live-claim case, and dedicated actions. Runtime-gaps QA: four nonowner denials + successful safe update; new pytest replicates. |
| **G2** | core/tasks.py, _update_from_kwargs: any non-null candidate_ref update after done OR incoming review_of returns candidate_ref_frozen, even if same value. | Generated task_manage/update documents both frozen conditions. QA confirms after done; new pytest also creates review_of and confirms incoming relation freeze. |

## Deterministic verification

- All **94 targeted pytest tests PASS** (original 85 plus 9 new), one Starlette/httpx deprecation warning. Full suite intentionally not rerun.
- Repeated independent Bravo-3 19-tool audit against the changed catalogue, with adapted script copies: **19/19 input and output schema parity**, 15 canonical runtime checks, 19 malformed tools/call HTTP cases plus one long-sentinel stress case. All complete ToolResult objects under 3000 bytes; **maximum 2207 bytes**, no schema echo or stress sentinel. Original QA scripts unchanged.
- Whitespace regression cases (archive, masked dual note, claim, release, comment) and ownership/candidate/runtime description checks PASS. The scripts themselves assert server behavior and published text separately; passing runtime-rejection probes does not mean whitespace input is accepted.
- Before vs after all **19 public tool records** are byte-equivalent for every field except description. inputSchema remains permissive; outputSchema, annotations, canonical action matrices and action model coverage unchanged.
- Exact strict sidecar SHA-256 **5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694**; baseline before-* artifacts unchanged; 10-file generator --check PASS; original QA directory diff -qr is empty.
- tiktoken 0.12.0 / cl100k_base: original transport baseline 57,254 bytes / 13,773 tokens; repaired candidate 60,028 bytes / 13,704 tokens (+2,774 bytes / +4.85%; -69 tokens / -0.50%). Compared with Echo-3 QA candidate 58,779 bytes / 13,460 tokens, completeness costs 1,249 bytes and 244 tokens. Tokenizer measurements are not ChatGPT billing.

**Proof location:** /workspace/terminal-mcp-artifacts/delta3-tmcp05-run01-20261010, including pytest-targeted-final.log, generate-final-check.log, independent-evidence.json, 19-tool-matrix.md, runtime-gaps.json, whitespace-regressions.json, scope-parity.json, rerun-*.py and rerun-*.log.

## Boundaries and remaining gates

No changes to canonical server contracts, handler behavior, storage, Events, OAuth, commands or runtime. No production DB, fleet or deployment touched.

MessageRecord.deliveries from separate SHA 409040524bc444894a4f092035cd5e2bc906b5c2 is **not integrated**. The common integrator must reconcile that output-contract change, regenerate schemas on the combined SHA and rerun model/output parity.

Next: coordinator independently reviews this published branch/SHA, selects merge order with concurrent changes, and schedules controlled ChatGPT live A/B for client pre-tool validation/schema echoes, authorized mutations, OAuth/admission, latencies and UX. **Live ChatGPT A/B and full suite were NOT TESTED here.** This handoff is not a deployment authorization.
