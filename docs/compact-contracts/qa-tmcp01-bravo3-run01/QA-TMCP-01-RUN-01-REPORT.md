# TMCP-01 / RUN-01 — independent acceptance of Echo-3 compact contracts

**Overall: BLOCKING — contract completeness FAIL; offline nonblocking transport and error compactness PASS; ChatGPT live A/B NOT TESTED.**
Reviewer Bravo-3 (secondary, legacy suffix `TMCP-01-QA`), session_ref `ws_oJZuT1N3L06XSM20TpzbsSQ4`, hard expiry 2026-10-10 22:57:53 UTC. Coordinator Zulu-2 suffix `dot` (active to 22:48:37 UTC) confirmed exclusive QA ownership. Exact detached checkout: `/workspace/qa-tmcp01-bravo3-20261010`.

- **Candidate SHA and remote branch HEAD:** `2649bc445252b8af9d34b6eee1dec2ce1ea6a601`, `task/TMCP-RKB4-COMPACT-B1-B13-echo3-20261010`, independently verified by git checkout and `git ls-remote`.
- **Strict-contract sidecar SHA-256:** `5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694`, exact Bravo-3 QA checksum; real FastMCP output/input models independently compared 19/19.
- Source documents read: `QA-REMEDIATION-ECHO3-20261010.md`, `HANDOFF.md`, `README.md`, `REPORT.md`, `QA-BRAVO3-20261010.md`; Echo-3 artifacts at `/workspace/terminal-mcp-artifacts/echo3-rkb4-b1-b13-20261010/` inspected. Developer 116 PASS was a claim, *not* an independent rerun count.

## Three-layer independent acceptance

1. Strict server Pydantic models: 19/19 exact match to strict inputSchema. Effective isolated FastMCP `create_app` output models: 19/19 exact match to sidecar outputSchema. Baseline flat public outputs unchanged.
2. Generated catalogues: all 19 effective tools/list candidate records match a separately instantiated isolated app after in-memory publication swap; all named input fields and required/optional markers match strict source model. Public transport objects expose every known name with `{}`, keep `additionalProperties:true`, and contain no required/type/bounds/value assertions (the root still carries descriptive and x-terminal-mcp-action-matrix metadata, which are not JSON Schema assertions). 9 task_manage action field policies and claim/release/comment/checkpoint policies inspected against canonical models. See `19-tool-matrix.md` and `independent-evidence.json`.
3. Runtime boundary: independent private SQLite data set (temp path), 15 canonical direct checks, task_state nonowner with existing owner preserves content revision 1→1 and keeps live claim, release without reason succeeds; 19 malformed plus 1 30-field/long-sentinel stress HTTP tools/call requests hit isolated app. All server result objects under 3000 bytes; maximum **2207 bytes** in this independent sample. No echoed inputSchema or stress sentinel. This proves only isolated MCP/server behaviour, **not client-side ChatGPT pre-tool rejection**. See `independent-audit.py`, log and JSON.

**Targeted tests:** `tests/test_compact_contracts.py`, `tests/test_compact_bravo3_independent.py`, `tests/test_compact_b1_b13_regressions.py`: **85 PASS**, 1 Starlette/httpx deprecation warning, exit code 0 (13.42 s). Generator `--check`: 10 deterministic artifacts PASS exit 0 after loading pinned tiktoken 0.12.0 from read-only prior QA dependencies. No full suite run.

## Manual 19-tool verdicts

| Role/tool | Source strict schema | Effective validation vs description | Verdict |
|---|---|---|---|
| access.session | IssuerSessionInput | action start/end, required enum, receipt | PASS |
| executor.session | AttachInput | required 4-digit session_number; attach-only default | PASS |
| executor.task_list | TaskListInput | filters, nullable fields, pagination | PASS |
| executor.command_run | CommandRunInput | required command, bounds, queue_id nullable | PASS |
| executor.command_read | MeshCommandReadInput | optional cmd_hash journal, cursor/limit | PASS |
| executor.command_cancel | CommandCancelInput | required cmd_hash 1..128 | PASS |
| executor.command_recovery | CommandRecoveryInput | required command 1..32768 | PASS |
| executor.task_claim | MeshTaskClaimInput and canonical Claim/Release | Exact allowed lists, optional release_reason; whitespace-only claim_intent/release_reason accepted by model yet rejected by core without explicit nonblank rule | **FAIL completeness** |
| executor.task_state | TaskStateInput | Nonowner state assignment works. Storage preserves CAS content revision without change; description does not state that effect | **FAIL completeness** |
| executor.task_comment | MeshTaskCommentInput and canonical Comment/Checkpoint | Correct cross-action exclusions, expected_revision forbidden; whitespace-only comment_text accepted by canonical model yet rejected by core without nonblank note | **FAIL completeness** |
| executor.message | MeshMessageInput | six actions, explicit null, local-only, scope=fleet disabled, targets/mode | PASS |
| coordinator.session | AttachInput | same as executor | PASS |
| coordinator.task_get | TaskGetInput | snapshot/detail/history, cursor | PASS |
| coordinator.task_list | TaskListInput | filters/defaults | PASS |
| coordinator.task_manage | TaskManageInput + nine canonical models | Allowed/required/forbidden lists and B1–B13 source anchors accurate where claimed; *additional runtime guards omitted*, and archive note wording false for whitespace alternative | **FAIL completeness** |
| coordinator.task_graph | TaskGraphInput | direction/kinds/pagination | PASS |
| coordinator.agent_observe | AgentObserveInput | no args, correct version-specific result | PASS |
| coordinator.message | MeshMessageInput | same six actions as executor | PASS |
| coordinator.health | HealthInput | extended flag and two result variants | PASS |

**Result: 15 PASS / 4 FAIL for complete client-facing contract; 19/19 PASS strict schema parity and transport projection.** Full per-field required/nullable matrix in `19-tool-matrix.md`. Claims of universally successful authorized mutations are NOT TESTED; valid-boundary calls may fail legitimately at domain/admission.

## B1–B13 verdict, one by one

| ID | Verdict | Independent finding |
|---|---|---|
| B1 | PASS | TaskUpdateRequest requires non-null expected_revision; action rules state it; missing and null fail. |
| B2 | PASS | All nine task_manage action allowed/required/forbidden field lists present and canonical-model-derived; top-level union no longer misrepresents field policy. |
| B3 | **FAIL** | Archive note-or-archive_note alternative works for normal text, but description says *either non-null is sufficient*. `note='   '` passes TaskArchiveRequest and fails core. Stronger: `archive_note='   ',note='valid fallback'` passes canonical and fails: truthy whitespace archive_note masks valid fallback note. |
| B4 | **FAIL (full value contract)** | Release_reason really is optional for live owner and omission succeeds; when supplied it must contain non-whitespace characters. Strict source string(1..4000) and generated text omit trimming rule; whitespace-only canonical-valid request fails. Original invented mandatory-condition defect is fixed. |
| B5 | **FAIL (description completeness)** | Any admitted agent can change task_state regardless of live claimant; independently tested. Content revision remains 1→1, unlike ordinary update; generated task_state description does not say this. Ownership part fixed. |
| B6 | **FAIL (full value contract)** | Claim/release action whitelists and non-null claim_intent correct; canonical-valid whitespace-only claim_intent fails `_clean_reason` at runtime. Required non-null string(1..160) is incomplete. |
| B7 | PASS | Null is presence-sensitive; explicit null forbidden-position values are accepted; action mode/target/local semantics described for both roles. |
| B8 | **FAIL (full value contract)** | Comment/checkpoint extra/null and expected_revision exclusions correct. Whitespace-only comment_text passes canonical and fails `_action_comment`; description states non-null, omits non-whitespace requirement. |
| B9 | PASS | Null-vs-presence conditional requirements, done result, archive note alternative at model level documented; *nonblank runtime gaps separately charged to B3/B4/B6/B8*. |
| B10 | PASS | `description:null` omitted, `tags:[]` replaces list; described and independently checked. |
| B11 | PASS offline; **NOT TESTED live** | All 19 transport schemas permit malformed object arguments to reach isolated RuntimeBoundary. 20 independent HTTP errors bounded with no schema echo; real ChatGPT UI/pre-tool behaviour unknown. |
| B12 | PASS | Task_comment checkpoint no expected_revision, unknown field rejected, stale advertised source removed. |
| B13 | PASS for original finding | relation_kind open string, review_of lane/candidate/unique-parent/binding constraints described; independent source check. Separate candidate_ref freeze gap below. |

**ID counts:** 8 PASS and 5 FAIL on full textual value/effect contract; B11 live gate untested.

## Additional independently reproduced blockers

**G1 — Live-ownership state machine invisible in task_manage.** `core/tasks.py:_owner_error` (around L530), `_action_archive` L1521, `_action_relate` L1375, `_action_unrelate` L1421, `_update_from_kwargs` L1632. On a private temp task claimed by `owner`, canonical-valid archive(note='reason'), relate, unrelate, and update(lane='review',expected_revision=1) by `nonowner` all return `owner_required`. Canonical-valid update(title='...') by nonowner succeeds as a safe participant edit. No live-owner/SAFE_PARTICIPANT_FIELDS distinction appears in generated coordinator.task_manage description. `runtime-gaps.py` exit0 proves all five paths.

**G2 — Candidate immutability missing.** `_update_from_kwargs` around L1609 rejects update(candidate_ref='candidate-2') for completed task with `candidate_ref_frozen`; Pydantic accepts and generated task_manage description does not disclose this. Same source protects tasks with incoming review_of. Independently reproduced in isolated DB.

**G3 — Non-null is not nonblank.** `_clean_reason` strips whitespace on claim_intent, release_reason, archive_note/note; `_action_comment` requires nonblank text. Canonical Pydantic length validators permit whitespace-only strings; generated descriptions advertise only non-null and length. See `whitespace-regressions.py`, five canonical-valid/runtime-rejected cases, exit0. In archive double-field case, a valid second note is masked by a whitespace-only first note.

**G4 — task_state effect not explicit.** Server `storage/tasks.py:set_workflow_state` independently changes workflow state, skips CAS content revision bump and allows another admitted agent to mutate state. Runtime proof: state ready→blocked, revision 1→1 with owner's claim still active. The executor.task_state generated description correctly removes owner restriction but fails to mention revision behaviour.

These are documentation/contract completeness blockers; implementation and existing handler behaviour were **not** altered. The contract should describe actual live ownership, candidate freeze and non-whitespace guards precisely. Archive dual-note precedence is a server behaviour whose client-safe rule must be documented or separately fixed by implementation owners.

## Measurements / limits

Pinned `tiktoken==0.12.0`, `cl100k_base`, sorted compact UTF-8 tools/list for all 3 catalogues: **57254→58779 bytes** (+1525, +2.664%), **13773→13460 tokens** (-313, -2.27%). Access 1120→2089 B / 269→467 tokens; Executor 16009→28334 B / 3678→6406; Coordinator 40125→28356 B / 9826→6587. These are tooling-tokenizer estimates, **not ChatGPT billing**. No byte reduction is claimed. The local server's errors were compact; no conclusion can be drawn about ChatGPT client-side validation, hidden schema echo, selection, live OAuth, latency, full authorized mutations or deployment.

New MessageRecord.deliveries from **separate candidate** `409040524bc444894a4f092035cd5e2bc906b5c2` is **absent by design** from audited SHA. Regenerate and independently compare all output models on any future integrated common SHA. Never combine current findings with 4090405's output schema until integration.

## Commands, exit codes, provenance

All run via @Solvenger secondary temporary legacy session, in detached `/workspace/qa-tmcp01-bravo3-20261010`, using `PYTHONPATH="$WT/src:/workspace/terminal-mcp-compact-qa-bravo3-20261010/.qa-deps"` and `/workspace/terminal-mcp/.venv-test/bin/python` (old QA dependency path *read-only*). All fixture DBs were isolated temp SQLite paths.

```sh
git -C /workspace/terminal-mcp ls-remote origin refs/heads/task/TMCP-RKB4-COMPACT-B1-B13-echo3-20261010  # 2649bc4... (exit 0)
git -C /workspace/terminal-mcp worktree add --detach /workspace/qa-tmcp01-bravo3-20261010 2649bc445252b8af9d34b6eee1dec2ce1ea6a601 # exit 0
cd /workspace/qa-tmcp01-bravo3-20261010
python scripts/build_compact_catalogs.py --check # PASS 10, exit 0 with pinned deps; first attempt exit 1 (missing tiktoken; then resolved via read-only prior QA deps)
python -m pytest -q tests/test_compact_contracts.py tests/test_compact_bravo3_independent.py tests/test_compact_b1_b13_regressions.py # 85 PASS, exit 0
python /workspace/terminal-mcp-artifacts/bravo3-tmcp01-20261010/independent-audit.py # PASS 19, 15 differential, 20 HTTP, exit 0 after fixing *own QA-script* fixture typos
python /workspace/terminal-mcp-artifacts/bravo3-tmcp01-20261010/runtime-gaps.py # PASS reproducible discrepancies, exit 0
python /workspace/terminal-mcp-artifacts/bravo3-tmcp01-20261010/whitespace-regressions.py # PASS reproducible discrepancies, exit 0
```

Exit-code files, logs, full scripts, source/remote SHA and per-tool evidence in same QA artifact folder. Each reproducible discrepancy test exits 0 **when it successfully demonstrates current contract failure**; it is not a product PASS.

## Acceptance gate / checkpoint

**STOP for integration/deploy; require corrected text-source annotations and new candidate SHA.** Developer to document (1) live owner vs safe participant on update/archive/relate/unrelate; (2) candidate_ref freeze for done and review_of; (3) nonblank constraints and archive dual-note precedence; (4) task_state no content revision bump. Then regenerate 10 artifacts, compare all 19 strict/input/output/description rows, rerun B1–B13 + new whitespace/ownership regression probes, verify compact HTTP errors and complete client A/B. When MessageRecord.deliveries is eventually integrated, rerun generation on the exact integrated SHA. Full suite and live ChatGPT A/B remain **NOT TESTED** in RUN-01.

No production DB, credentials, running processes, implementation branches, deployment, or integration were modified. QA report/evidence only; legacy MCP session must be ended normally after publishing status.
