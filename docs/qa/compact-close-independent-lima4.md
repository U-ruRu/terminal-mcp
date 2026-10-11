# Compact/chunk independent acceptance — Lima-4

## Scope and provenance

Implementation source under initial review: `9456518991c31daf99b20a209e684a7ab3a201a6`,
parent `d96b682045744a9127140bf8b494afecd2a33184`. Remote verified 11 October 2026.
The old QA commit `4f43db630b38d75ad245314e8d26e7be0d427a71` is not an ancestor;
its two additional test files were imported byte-for-byte. Current canonical
`a1a2a45f0199a2e50fbc322f938068e00bf76823` is also not an ancestor.

Reciprocal native messages and verified separate claims establish Kilo-4 as sole
implementer, Lima-4 as independent reviewer. Mike-4 explicitly confirmed the role
assignment as dot's current relay. No implementation, runtime, Events, canonical,
subscription, deployment or other worker checkout edits are made by this QA branch.

Read the previous implementation and Hotel-4 handoffs. The chunk artifact directory
contains candidate.sha, remote-verified.txt, focused log/exit/JUnit and generated
checks; no current chunk-specific HANDOFF file was found there or in its worktree.
Its tracked docs/compact-public-responses.md and commit provide the implementation
contract. The saved COMPACT_RELAY_HANDOFF.txt records the earlier HOLD and preserved
H4 requirements, not a completed acceptance of 945651.

## Independent requirements matrix

| Requirement | Initial exact-945651 result | Evidence / boundary |
|---|---|---|
| H4-B1: root-only error obligations; no obsolete duplicate aliases | FAIL | 4 unchanged Hotel-4 cases |
| H4-B2: legal 501 / 16384-character text, full 70-character slot suffix | FAIL | 6 unchanged Hotel-4 cases |
| command_run immediate acceptance, actual states, no inline output | PASS | Frozen preexisting independent oracle |
| command_read output, status, known exit, cancellation, retention, cursor | PASS | Frozen preexisting independent oracle |
| Long incoming notices with all four command operations | PASS / FAIL | Accepted receipts pass; 8 refusal cases fail H4 normalization |
| Aggregate output + incoming + mirrored JSON byte budget | FAIL | L4-B3: completed recovery receipt lost; false not_committed |
| Already committed canonical error with extra receipt/notices | FAIL | L4-B4: committed becomes unknown on run/recovery |
| Chunk reconstruction through real public wire | PASS | Independent mixed, escaped and stored astral text across notify/ack/alert |
| text_offset/text_limit, empty exact-end, past-end and negative offset | PASS | Independent public boundary; strict prior author cases also run |
| Full UTF-8/code-point integrity and receipt semantics | PASS | Independent byte equality, unchanged stored body, ACK/reply timestamps |
| Newly sent oversized 65536-byte astral body | PASS | Existing preflight rejects before writing, truthful not_committed |
| task_claim minimal success and conditional notices | PASS | Existing independent wire oracle |
| task_get detail/snapshot/history, typed opaque user JSON | PASS | Existing independent oracle and SQLite invariants |
| History cursor, graph partition and review gates | PASS | Existing history, revision, checkpoint and task_refs tests |
| Short/extended health and meaningful false/0/null | PASS | Existing independent oracle |
| Names on already-authoritative lookup paths | PASS | Existing oracle; no invented allocator/fallback |
| Current-canonical complete native names and intent | NOTTESTED | Separate implementation absent; two intent cases SKIP |
| Final integration/subscription compatibility | NOTTESTED | No merge, deployment or live subscription work authorized |
| One final full suite | NOTTESTED | Await explicit single owner/SHA approval |

## New actionable findings

L4-B3: a recovery result containing 22 lines of 1024 ASCII characters and four
maximum-length control-character notifications passes each constituent size
expectation but exceeds the final mirrored CallToolResult budget. The record
already exists in isolated SQLite as completed. The old serialization guard
returns output_item_too_large / not_committed and drops cmd_hash. Preserve the
receipt and truthful committed outcome while bounding the *whole* wire response.

L4-B4: an already canonical internal_error with outcome=committed, a persisted
cmd_hash and long incoming notices is re-normalized into outcome=unknown. Both
command_run and command_recovery public boundaries reproduce this. An attached
notice must not weaken a known commit outcome or erase the recovery identifier.

Both are deterministic isolated projection cases, not production command execution.
The existing backend recovery reads up to 500 stored lines, so the combined-output
budget case is relevant beyond a deliberately oversized single line.

The initial all-astral reconstruction setup tried sending a new 65536-byte body.
That was rejected by the existing admission preflight before storage. Those three
setup failures are not classified as chunk defects. Corrected fixtures explicitly
seed a historical body in their private SQLite database and independently test
truthful preflight rejection with no writes. No runtime state is used.

## Reproduce focused acceptance

From this QA branch (production source remains exact 945651):

```sh
PYTHONPATH=src python -m pytest \
  tests/test_compact_response_independent.py \
  tests/test_compact_error_notices_independent.py \
  tests/test_compact_large_messages_independent.py \
  tests/test_compact_close_independent.py \
  tests/test_message_text_chunks.py tests/test_compact_incoming_budget.py \
  tests/test_task_history_cursor_stability.py \
  tests/test_task_checkpoint_append_boundary.py \
  tests/test_task_revision_side_effects.py tests/test_task_refs.py \
  -q --tb=short -o junit_family=xunit1 --junitxml=focused.xml
```

The new test module has 31 cases. JUnit properties retain exact byte sizes,
original UTF-8 lengths and chunk counts. Tests remain reviewer-owned; do not
weaken the expectations to turn an uncorrected implementation green.

Short success examples remain `{ok:true}` for task_claim and root
ok/remaining_seconds/cmd_hash/status/queue_id for command_run. Clients need
command_read for command/output/exit details; message(action=read,message_hash=ID,
text_offset=...,text_limit=...) reads bounded full text. A preview warning must
name an actionable continuation. It never substitutes for ACK or reply.

Artifacts: `/workspace/terminal-mcp-artifacts/compact-close-qa-lima4-20261011/`.
Initial baseline: 117 PASS / 10 FAIL / 2 SKIP, exit1 (129 cases including 17 supplied
chunk cases). Expanded independent run: 20 PASS / 8 H4-related FAIL. Additional
L4-B3 and L4-B4 probes add 1 and 2 failures. These disjoint extensions yield a
160-case final focused baseline; its unified log/exit/JUnit is captured separately.
No duplicate full suite is run.
