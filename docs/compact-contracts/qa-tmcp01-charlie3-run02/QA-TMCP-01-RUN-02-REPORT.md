# TMCP-01 / RUN-02 — independent offline acceptance of Delta-3

**Final verdict: NON_BLOCKING for the audited offline contract candidate; live ChatGPT A/B NOT TESTED.**
Run-01's B3/B4/B5/B6/B8 and additional G1/G2 are closed in the generated descriptions on **exact SHA** `baaf7744a94ca126ba0b913b6829f98e2a33b693`. B1–B13: 13 offline PASS. G1/G2: 2 offline PASS. All 19 tool records: source-schema/strict-output/descriptive parity PASS. There is **no deployment or integration approval** in this verdict.

## Identity, publication and source scope

- Reviewer **Charlie-3**, legacy Secondary @Solvenger, suffix `TMCP-01-RUN-02-QA`, session_ref `ws_PBt5XZdS3iUezp9_lSM_DgIT`, expiry **2026-10-10T23:32:22.012Z**.
- Coordinator **Zulu-2**, suffix `dot`, renewed session **2026-10-10T23:08:50.088Z–23:31:50.088Z**, confirmed exclusive TMCP-01/RUN-02 QA.
- Common integrator TMCP-07 **Bravo-3**, independent current session to **2026-10-10T23:29:35.876Z**. This reviewer is Charlie-3, not RUN-01's earlier Bravo-3 nor the TMCP-07 integrator.
- Candidate branch `task/TMCP-05-RUN-01-delta3-20261010`; independent `git ls-remote` HEAD **baaf7744a94ca126ba0b913b6829f98e2a33b693**. Its single parent is **2649bc445252b8af9d34b6eee1dec2ce1ea6a601**.
- Our own checkout `/workspace/qa-tmcp01-run02-charlie3-20261010`, initially detached at exact SHA, later QA-only branch on this parent. Unmodified developer branch and unchanged server runtime.
- Prior independent QA RUN-01 SHA **36914f1fd19518ea0c40afb5eceadca0086230ca**: 24/24 existing `docs/compact-contracts/qa-tmcp01-bravo3-run01/` files compared byte-by-byte against that QA commit and confirmed unchanged.
- Read `docs/compact-contracts/HANDOFF-TMCP-05-RUN-01-20261010.md`, `CHECKPOINT-TMCP-05-RUN-01-20261010.md`, prior QA report and source proof, plus reviewed both changed compiler/annotation files. Delta-3 developer proof folder `/workspace/terminal-mcp-artifacts/delta3-tmcp05-run01-20261010` was available but treated as a claim to verify, not acceptance evidence.

**Source scope:** the only modified files under `src/` are `mcp/compact_annotations.json` (source-linked human-readable runtime rules) and `mcp/compact_contracts.py` (action-scoped annotation rendering/validation). **No modifications** to Pydantic request models, output models, RuntimeBoundary, handlers, core/tasks, storage, OAuth, Events or active MCP deployments.

## Strict/public contract immutability (independently verified)

1. **All 19 published tool records** in candidate catalogues were diffed *field by field* against source parent `2649bc4`. Every field except `description` is byte-identical: inputSchema, outputSchema, name, annotations and any extension metadata. Only four descriptions changed: `executor.task_claim`, `executor.task_state`, `executor.task_comment`, `coordinator.task_manage`. Other fifteen remain byte-identical, preserving previously accepted behaviour.
2. Public `inputSchema` for all 19: `type=object`, known argument names mapped to `{}`, `additionalProperties=true`, no client-side required/type/value/bounds assertions. Public flat outputSchema remains unchanged on 19/19.
3. `strict-contracts.json` SHA-256 = **5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694**, exactly source-parent and prior QA. `before-{access,executor,coordinator}.json` likewise byte-for-byte identical to parent.
4. Independent isolated `create_app` introspection: source Pydantic input schema and FastMCP output-model schema match strict sidecar **19/19**; proposed `tools/list` exactly matches all generated catalogue entries after in-memory-only publication swap. Required textual markers, 19 field rosters and transport/output projection checked independently.
5. Exact per-operation policy lists in description independently reconstructed from canonical action Pydantic fields. **Nine** coordinator.task_manage actions, **two** task_claim actions, **two** task_comment actions match per-action **allowed / required-non-null / forbidden** lists. Compiler checks action scope against canonical registry; actual runtime annotations were checked against source-file anchors and their presence in the generated descriptions.

Supporting evidence: `verify-parity.py`, `scope-parity-independent.json`, `manual-contract-review.py`, `b-g-manual-adjudication.json`, `manual-19-tool-matrix.md`, `independent-audit-rerun.py` and `independent-evidence.json`.

## Manual disposition of all nineteen tools

| Effective publication | Strict source / description assessment | Verdict |
|---|---|---|
| access.session | IssuerSessionInput: required start/end, session issuance/end | PASS |
| executor.session | AttachInput: four-digit session_number required, attach only | PASS |
| executor.task_list | TaskListInput: read filters/defaults/nulls, bounded pagination | PASS |
| executor.command_run | CommandRunInput: command required; queue_id/task_scope bounds | PASS |
| executor.command_read | MeshCommandReadInput: optional cmd_hash journal and pagination | PASS |
| executor.command_cancel | CommandCancelInput: nonempty cmd_hash | PASS |
| executor.command_recovery | CommandRecoveryInput: nonempty command | PASS |
| executor.task_claim | MeshTaskClaimInput + TaskClaimRequest/TaskReleaseRequest; **B4/B6 repaired**; omission/null reason vs supplied nonblank, invalid cross-action extras | PASS |
| executor.task_state | TaskStateInput: admitted nonowner may set state, content revision unchanged, live claim persists, idempotent reassign no event | PASS |
| executor.task_comment | MeshTaskCommentInput + canonical comment/checkpoint; nonblank text, forbidden checkpoint extras, no expected_revision | PASS |
| executor.message | MeshMessageInput: send/ack/reply/read/history/recipients, explicit null, local scope/mode | PASS |
| coordinator.session | AttachInput: same four-digit attach-only requirement | PASS |
| coordinator.task_get | TaskGetInput: snapshot/detail/history and cursor semantics | PASS |
| coordinator.task_list | TaskListInput: filters/defaults/nulls, bounded pagination | PASS |
| coordinator.task_manage | TaskManageInput + **nine** action-specific canonical models; B3/B5/B8/G1/G2 now accurately rendered, other earlier fixes preserved | PASS |
| coordinator.task_graph | TaskGraphInput: direction/kinds/limit/cursor | PASS |
| coordinator.agent_observe | AgentObserveInput: no args, correct strict response | PASS |
| coordinator.message | MeshMessageInput: same six message actions and nullable/scoping semantics | PASS |
| coordinator.health | HealthInput: default/extended and output alternatives | PASS |

All 19 comparisons are **offline** and include strict models, effective isolated server metadata, descriptions and sampled runtime validation. See `manual-19-tool-matrix.md` for explicit field counts and strict required names per tool.

## B1–B13: individual verdicts

| Finding | Verdict | Independent contract evidence |
|---|---|---|
| B1 | PASS | update expected_revision present, required non-null; missing/null canonical rejected |
| B2 | PASS | Nine task_manage per-action lists exactly model-derived (allowed/required/forbidden), no union-as-allowed overclaim |
| B3 | PASS | Runtime raw archive_note precedence, strip-to-nonblank rule and masked valid note case now expressly described. Canonical non-null alternative separately identified |
| B4 | PASS | release_reason optional on live release; absent/null defaults to released_by_owner; supplied whitespace rejected; idempotent not-claimed release bypasses reason parsing |
| B5 | PASS | task_state available to admitted nonowner despite another live claim; content revision CAS not used/bumped; claim survives; repeated assignment emits no event |
| B6 | PASS | claim_intent required non-null **and nonblank**; claim/release cross-action fields excluded; action-scoped descriptions correct |
| B7 | PASS | message explicit null/absence, mode and local scope conditions retained byte-for-byte; validators untouched |
| B8 | PASS | executor.task_comment and coordinator.task_manage/comment demand non-whitespace comment_text; checkpoint forbids expected_revision and cross-action extras |
| B9 | PASS | canonical action-specific required non-null conditions preserved; runtime nonblank details supplemented |
| B10 | PASS | update description:null omitted and tags:[] replaces, as in prior source/runtime proof |
| B11 | **PASS offline / live NOT TESTED** | All 19 object transport schemas permissive; isolated invalid calls return bounded server errors. Actual ChatGPT client pre-tool rejection untested |
| B12 | PASS | checkpoint's stale advertised expected_revision removed; unknown field server-forbidden |
| B13 | PASS | relation_kind remains open 1..64 string; review_of lane, parent candidate and uniqueness runtime conditions retained |

### G1 — ownership/safe participant rule: PASS

Source `core/tasks.py`: `SAFE_PARTICIPANT_FIELDS`, `_owner_error`, `_update_from_kwargs`, archive/relate/unrelate handlers. With another live owner, nonowner `archive`, `relate`, `unrelate` and workflow-changing `update(lane=review)` return `owner_required`. Safe `update(title=...)` succeeds. Independently tested safe title alongside `force=true`, `force_reason` and `candidate_ref=null`: succeeds, so the safe-participant wording correctly refers to actual content mutation and does not hide legitimate control flags. Without a live claim, archive/relate/unrelate succeed. A stale content revision still conflicts on update. These correct distinctions are included in task_manage runtime rules.

### G2 — candidate_ref freeze: PASS

A non-null `candidate_ref` update on completed task yields `candidate_ref_frozen`, including an unchanged value. Separately, an actual source review task linked `review_of` to a parent with valid candidate_ref; updating the parent's candidate_ref to its **existing** value also yields `candidate_ref_frozen`. Passing `candidate_ref=null` omits the update, allowing a safe title change. Both frozen conditions, even-equal guard and nullable omission are documented.

### B3/B4/B5/B6/B8 runtime boundary: PASS

The original independent RUN-01 whitespace probes were rerun against unchanged core on fresh disposable SQLite:
- `archive_note='   '`, including simultaneous `note='valid fallback'`, is canonical-valid but runtime-rejected. New text explains precedence and failure; meaningful note-only archive succeeds.
- `claim_intent='   '`, supplied live-claim `release_reason='   '`, and `comment_text='   '` are canonical-valid but runtime-rejected as intended; new descriptions specify nonblank constraints. Release with omitted reason succeeds, and a separate unclaimed idempotent release bypasses whitespace reason validation.
- Another admitted agent changes workflow state despite a foreign claim and deliberately stale **core-only** expected_revision, without incrementing stored content revision or releasing claim. Repeated state assignment produces no event. **The public executor.task_state input does not advertise expected_revision**; that core-only probe tests absence of CAS guard, not acceptance of an extra public field.
- Canonical input/null/extra limits from B1–B13 remain unchanged; the descriptions represent them without a new false validation requirement.

Evidence: `rerun-runtime-gaps.py`, `rerun-whitespace-regressions.py`, `adversarial-runtime.py` and their JSON/log/exit files.

## Tests, deterministic build, error size

**Targeted pytest (independently rerun):** `tests/test_compact_contracts.py`, `test_compact_bravo3_independent.py`, `test_compact_b1_b13_regressions.py`, `test_compact_tmcp05_completion.py` — **94 PASS**, **exit 0**, 16.02 seconds, 1 known Starlette/httpx deprecation warning. The original RUN-01 QA tests are untouched.

**Independent 19-tool isolated MCP audit:** PASS exit 0, 19/19 input+output/description/transport/effective catalogue parity; 15 independent canonical cases, 19 malformed JSON-RPC `tools/call` operations and one large sentinel stress test. All errors produced compact structured server results; largest complete ToolResult **2207 UTF-8 bytes** in this 20-sample replay. No full schema or stress-secret marker returned. These errors were generated on an isolated FastAPI/TestClient app with prospective catalogues installed in-memory; **ChatGPT client pre-tool validation, UI schema echoes, billing, latency and UX are NOT TESTED**.

**Deterministic generator:** `scripts/build_compact_catalogs.py --check` exit 0, **PASS 10 deterministic compact contract artifacts**. The generator and all tests import candidate source through `PYTHONPATH=$WT/src`. Pinned `tiktoken==0.12.0` loaded from prior read-only QA dependency folder, no production pip changes.

**Complete tools/list JSON metrics** (`tiktoken 0.12.0`, `cl100k_base`, compact sorted UTF-8, not ChatGPT usage billing):

| Connector | Bytes baseline → candidate | Tokens baseline → candidate |
|---|---:|---:|
| Access | 1,120 → 2,089 | 269 → 467 |
| Executor | 16,009 → 28,534 | 3,678 → 6,443 |
| Coordinator | 40,125 → 29,405 | 9,826 → 6,794 |
| **Total** | **57,254 → 60,028 (+2,774 / +4.845%)** | **13,773 → 13,704 (-69 / -0.501%)** |

Descriptions became more complete; no byte saving is claimed. The former Echo-3 candidate was 58,779 bytes / 13,460 tokens; do not reuse the old 2.27% saving for Delta-3.

## Exact reproducible commands / final exit codes

Run in independent checkout; no production DB/processes touched:

```bash
WT=/workspace/qa-tmcp01-run02-charlie3-20261010
OUT=/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010
export PYTHONPATH="$WT/src:/workspace/terminal-mcp-compact-qa-bravo3-20261010/.qa-deps"
cd "$WT"
PY=/workspace/terminal-mcp/.venv-test/bin/python

git ls-remote origin refs/heads/task/TMCP-05-RUN-01-delta3-20261010
$PY scripts/build_compact_catalogs.py --check
$PY -m pytest -q tests/test_compact_contracts.py tests/test_compact_bravo3_independent.py tests/test_compact_b1_b13_regressions.py tests/test_compact_tmcp05_completion.py
/usr/bin/python3 "$OUT/verify-parity.py"
$PY "$OUT/independent-audit-rerun.py"
$PY "$OUT/rerun-runtime-gaps.py"
$PY "$OUT/rerun-whitespace-regressions.py"
$PY "$OUT/adversarial-runtime.py"
$PY "$OUT/manual-contract-review.py"
```

Every final command above **exit 0**, with logs plus `*.exit` files and machine-readable JSON in independent QA artifact folder. The custom `manual-contract-review.py` initially assumed annotations never used wildcard role keys, causing **exit 1 in our test harness**, then was corrected to verify both executor/coordinator wildcard expansions; final full rerun **exit 0**. No candidate code was changed to fix that QA-harness issue. Tests specifically proving that server rejects malformed business values exit 0 when those rejections match documented rules.

## Limits and next gates

This **NON_BLOCKING** verdict applies **only** to the exact offline Delta-3 SHA. The missing optional `MessageRecord.deliveries` field from the separate candidate **409040524bc444894a4f092035cd5e2bc906b5c2** is expected and **not a blocker** for baaf774's present correctness. On a later *combined exact SHA*, the integrator must regenerate all catalogue output models and repeat strict and public tool parity; do not silently combine these independent SHAs.

The full general project suite, live ChatGPT connector A/B, actual pre-tool validator/schema echo, authenticated successful live mutations, OAuth/Events deployment, latency, usage billing, rollout and rollback are **NOT TESTED / NOT DONE** here. TMCP-07 owns full integration and suite sequencing. This reviewer did not change any active server, databases, credential, command service, unrelated branch, integration or deployment.

**Coordinator Zulu-2 and integrator Bravo-3 were informed promptly of independently substantiated preliminary NON_BLOCKING at 23:12 UTC** to unblock their suite planning. The QA-only publication and final checkpoint follow. The legacy MCP session will end normally before expiry.
