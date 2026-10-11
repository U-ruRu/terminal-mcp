# Independent compact-response review: 7ffc0b2

## Scope and provenance

Candidate: `7ffc0b28896f8d12b17ec0df4b05c00b3cbc236d`.
Implementation branch: `task/TMCP-COMPACT-RESPONSES-yankee3-20261011`.
Canonical base: `83677e52660d8c1cc21c10ac0ce59bf6b1fa1b82`.
Independent fixtures: `adaa36442c4daf7119b1c9f680e9d4568283e3a9`.
Implementer: Yankee-3. Reviewer: Zulu-3, continued as Delta-4 after legacy expiry.
Both roles and native claims were confirmed before edits. Review stayed separate.

**Verdict: tested compact-contract scope PASS; overall release HOLD.**
The verdict applies to the exact candidate SHA, not any subsequent branch tip.
No canonical promotion, runtime update, deployment, or full suite was performed.

## Independent execution

At 2026-10-11 14:56:10 UTC: **81 PASS, 2 SKIP, 0 FAIL, pytest exit 0**.
Duration: 24.09 seconds. Composition: 54 independent cases (52 PASS, 2 intent
SKIPs) plus 29 existing history/checkpoint/revision-side-effect invariants.

Source was fetched and remote SHA verified, then checked out independently at
`/workspace/terminal-mcp-worktrees/compact-accept-7ffc0b2-zulu3` (detached, clean).
Frozen independent fixtures remained outside that checkout with SHA256 provenance.

Artifacts: `/workspace/terminal-mcp-artifacts/compact-qa-zulu3-20261011/`.
Files: `candidate-7ffc0b2.log`, `candidate-7ffc0b2.xml`, `candidate-7ffc0b2.exit`,
`candidate-provenance.txt`, `candidate-static.log`, `review_tests/PROVENANCE.txt`,
`examples-before.json`, `examples-after.json`, `render_examples.py`, `HANDOFF.md`.

Reproduce from the candidate checkout:

```sh
A=/workspace/terminal-mcp-artifacts/compact-qa-zulu3-20261011
PYTHONPATH=src /opt/terminal-mcp-src/.venv/bin/python -m pytest \
  "$A/review_tests/test_compact_response_independent.py" \
  tests/test_task_history_cursor_stability.py \
  tests/test_task_checkpoint_append_boundary.py \
  tests/test_task_revision_side_effects.py \
  -q --tb=short --junitxml="$A/candidate-7ffc0b2.xml"
```

## PASS / FAIL / NOTTESTED matrix

| Area | Result | Evidence |
| --- | --- | --- |
| command_run | PASS | Exact root keys, all five statuses, meaningful remaining_seconds=0 |
| Immediate acceptance | PASS | Independent fake scheduler forbids status-poll sleeps |
| Incoming messages | PASS | notify/ack/reply, aliases dedup, root-only run/read/claim, unchanged source data |
| task_claim | PASS | Ordinary ok-only; required notices; real SQLite revision conflict without mutation |
| task_get detail | PASS | Removed fields, nonempty service collections, opaque user JSON, compact claims |
| Task links | PASS | Blocking/other relation partition without changing supplied graph |
| task_get snapshot | PASS | Stored dict/list/string/empty values, JSON-looking strings, metadata removal |
| Histories | PASS | Event fields reduced, payload intact, comments unchanged |
| History continuation | PASS | Present cursor, stable pages, explicit same-stream invalidation, restart, >100 events |
| command_read | PASS | command.cmd/error, stdout/stderr lines, known exit0/nonzero, finish/cancel, retention/cursor |
| health | PASS | Exact compact keys, full extended diagnostic dictionaries, degraded state preserved |
| Errors and run strict schema | PASS | Small actionable recovery details; valid new shape and forbidden old fields |
| Native Mesh public names | NOTTESTED / HOLD | Exact shared naming candidate pending; original source derives issuer-hash names |
| Combined intent | NOTTESTED | Two explicit SKIPs until coordinator-approved integration |
| Final combined full suite | NOTTESTED | No agreed final owner/base, no full suite started |
| Wider legacy expectations | NOT independently accepted | Author reported152PASS/15FAIL and is preparing updated expectations plus a missing-status exception fix |

No test in the independent 83-case candidate run failed. Earlier baseline red
controls deliberately applied the new oracle to old production code; they are
not failed candidate acceptance runs and must not be merged into this verdict.

## Static checks

Independently PASS, exit0: Ruff src, 10 deterministic generated compact artifacts,
Events catalog, repository privacy and diff whitespace. Candidate author and
committer: `Terminal MCP Contributors <tmcp-contributors@example.invalid>`.
Catalog generation uses the existing project tiktoken shim path. Its statistics
show total catalog bytes +14.832% versus its own before snapshot. That is separate
from public response size; no claim is made that total tool context decreased.

## Reproducible examples

Same synthetic ordinary-success fixtures, serialized structured JSON (UTF-8,
compact separators), baseline versus candidate: command_run **815 -> 86 bytes**;
task_claim **529 -> 11 bytes**. These are example measurements, not a workload benchmark.

Candidate command_run:
```json
{"ok":true,"remaining_seconds":0,"cmd_hash":"aabbccdd","status":"queued","queue_id":3}
```
Candidate task_claim:
```json
{"ok":true}
```
Complete before/after examples and rendering script are retained with the artifacts.

## Proven semantics and remaining gates

Persistent messages/pending_messages are identical incoming-obligation aliases;
legacy pending entries are formatted strings of the structured incoming records.
They are not outgoing delivery receipts. Pre-change findings were posted TO DOT
on the QA board. Serialization never gains ACK/reply authority.

Checkpoint storage already retains JSON types; the old projection stringified,
extracted a text key or truncated them. Independent SQLite fixtures compare stored
rows and events before/after the public snapshot read.

The existing task.next_cursor is snapshot-bound: changes to the selected history
reject stale offsets explicitly. Stable streams continue, and restart recovers all
records. This did not require an assumed cursor repair or event deduplication change.

At14:56:43 UTC, Bravo-4 relayed dot's instruction: compact responses are a separate
next release; another current-release worker owns NATO naming. No allocator or
identity duplication. Name-facing acceptance awaits that exact shared candidate.
The independent checkpoint was sent back through Bravo-4 before its session ended.

Actual intent warnings originate in the outer role activity wrapper AFTER
projection and add both response metadata and a TextContent notice. Earlier raw
application-meta fixtures were corrected accordingly. The replacements stay
SKIP/NOTTESTED on a standalone candidate lacking intent.

A successor addressing older expectations and the author-reported missing-status
exception mismatch needs its own exact-SHA verification. Final names + intent +
subscriptions require one approved integrator and one full-suite owner, with log,
exit code and JUnit. A passing focused review does not approve deployment.

## Client compatibility

Move command_run hash/status/timer reads to root; use command_read for body/output/
exit. Move ordinary incoming messages to root and consume id/sender/text/requirements.
Fetch task_get after claim when task details are needed. Update removed task fields,
public-name handling, typed latest_checkpoint.checkpoint and the two graph collections.
Keep history request selectors despite removal of response detail/kind. Request
extended health for diagnostics. Refresh installed connector catalogs after the
separately accepted deployment; OAuth reconnection is not part of this change.

## Review-session bookkeeping

The first legacy session expired after evidence was saved. Reclaiming its QA task
from the successor session returned task_claim_conflict; the retained old claim was
not forcibly changed. A separate documentation-only task
`terminal-mcp/TMCP-COMPACT-QA-HANDOFF-20261011` was natively claimed and verified to
publish this already-completed review. It does not change implementation ownership.
