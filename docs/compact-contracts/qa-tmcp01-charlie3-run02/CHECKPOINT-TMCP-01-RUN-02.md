# CHECKPOINT — TMCP-01 / RUN-02, Charlie-3, Secondary

**Final status: NON_BLOCKING on exact Delta-3 offline SHA. No remaining independent RUN-02 QA blockers.** This checkpoint represents completed verification, not live/deployment acceptance.

## Identity and source

- QA name: **Charlie-3**, suffix `TMCP-01-RUN-02-QA`.
- Legacy MCP Secondary session: `ws_PBt5XZdS3iUezp9_lSM_DgIT`, started `2026-10-10T23:09:22.012Z`, expires `2026-10-10T23:32:22.012Z`; must be ended with `@Solvenger secondary session(action=end)`.
- Current coordinator: **Zulu-2**, suffix dot, newly renewed session `2026-10-10T23:08:50.088Z` to `2026-10-10T23:31:50.088Z`. Assigned exclusive QA to Charlie-3.
- Common integrator TMCP-07: **Bravo-3**, separately active; do not confuse with old RUN-01 QA agent Bravo-3.
- Candidate SHA: `baaf7744a94ca126ba0b913b6829f98e2a33b693`, branch `task/TMCP-05-RUN-01-delta3-20261010`, parent `2649bc445252b8af9d34b6eee1dec2ce1ea6a601`. Original QA RUN-01 SHA `36914f1fd19518ea0c40afb5eceadca0086230ca`.
- Independent worktree: `/workspace/qa-tmcp01-run02-charlie3-20261010`. QA artifacts: `/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010`. QA-only branch: `qa/TMCP-01-RUN-02-charlie3-20261010` (consult remote ref for published QA commit).
- Confirmation: candidate remote HEAD matches exact SHA; 24 original QA files preserved byte-for-byte; server/runtime source unchanged; strict sidecar SHA256 `5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694`.

## Finished scope and evidence

- B1–B13 **all PASS offline**; G1/G2 **both PASS** (includes nonblank release/claim/comment, archive dual-note mask, safe participant/nonowner, owner_required for workflow/relations/archive, candidate frozen done/incoming review_of, null candidate omission, state no CAS/revision increment, existing claims retained).
- 19/19 exact input and output source-schema parity, descriptions and required field markers, permissive public transport envelope, unchanged public output schema, effective isolated `tools/list` equality.
- 9 + 2 + 2 action-specific allowed/required/forbidden lists exactly match source canonical model fields, source-linked annotations and action scopes validated.
- All 19 public records **identical except description** to original Echo-3 parent; only 4 descriptions changed and passed independent re-review. No new server validator, handler, storage, OAuth, Events or environment changes.
- 94 targeted pytest **PASS, exit0** (16.02s, one known Starlette/httpx deprecation warning). 10 deterministic artifacts `--check` **PASS exit0**.
- Independent HTTP TestClient 19 invalid + one long-sentinel stress `tools/call` requests return compact results, max complete ToolResult **2207 bytes**; no schema/sentinel leak. 15 canonical differential checks PASS.
- New 12+ disposable SQLite adversarial scenarios PASS, including unclaimed positive branch, safe update with control flags, idempotent release, unchanged state revision, no event on repeated assignment, incoming review_of freeze and same-value failure. Logs and machine-readable evidence in output folder.
- **Live ChatGPT connector pre-tool rejection/schema dumps, authorized live mutations, performance, billing, deployment and full general suite: NOT TESTED.** Integration remains TMCP-07's responsibility.
- Net entire catalogue: **57,254 -> 60,028 UTF-8 bytes** (+4.845%), **13,773 -> 13,704 cl100k_base tokens** (-69 / -0.501%), from tiktoken 0.12.0. No byte saving claimed. Not live billing.
- `MessageRecord.deliveries` from separate `409040524bc444894a4f092035cd5e2bc906b5c2` intentionally absent. It is not a blocker on this SHA. Re-run schema generation/output-model parity on later integrated common SHA.
- Coordinator and integrator received initial NON_BLOCKING direct messages as soon as supported. Send final publication ref after QA push, then end MCP session.

## Commands to rerun the completed offline QA

```bash
WT=/workspace/qa-tmcp01-run02-charlie3-20261010
OUT=/workspace/terminal-mcp-artifacts/charlie3-tmcp01-run02-20261010
export PYTHONPATH="$WT/src:/workspace/terminal-mcp-compact-qa-bravo3-20261010/.qa-deps"
PY=/workspace/terminal-mcp/.venv-test/bin/python
cd "$WT"
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

All eight QA/test commands final **exit=0**. `manual-contract-review.py` initially exited 1 due incorrect local audit harness assumption about wildcard annotation keys; corrected the own QA script to check both affected roles, final rerun exit0. No candidate or runtime source changes.

## Remaining non-QA work / stop point

1. Integrator Bravo-3 on **new exact integrated SHA** reconciles separate `MessageRecord.deliveries` output candidate, reruns generator `--check`, strict outputs and public 19-tool equality, then full suite at controlled resource window. Do not call the present 94 targeted tests a full suite.
2. Arrange **fresh live ChatGPT Access/Executor/Coordinator connector A/B** to validate tool choice, malformed request paths, absence of *pre-tool* huge schema echo, authorized outcomes, live billing, latency, UX. Local TestClient cannot prove client behaviour.
3. Obtain a separate integration/release/deploy authorization. No deployment, production DB/key/process/branch mutations were part of this QA.
4. No further RUN-02 steps required on `baaf774`. Future QA begins on newly integrated SHA, not silently on this existing worktree.

See `QA-TMCP-01-RUN-02-REPORT.md`, `manual-19-tool-matrix.md`, `b-g-manual-adjudication.json`, `command-manifest.tsv` and all runnable independent scripts/exit/log files in the QA folder.
