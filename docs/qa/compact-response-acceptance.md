# Compact response independent acceptance matrix

Baseline: `83677e52660d8c1cc21c10ac0ce59bf6b1fa1b82`.
This matrix is defined before the implementation candidate. Tests must exercise
published candidate code in an independent checkout. An author's test run is
not independent acceptance. A final report supplies exact SHA, JUnit, log and
exit code. Only one owner runs the final combined full suite.

## Requirements and independent evidence

| ID | Requirement | Independent check | Initial status |
| --- | --- | --- | --- |
| C01 | No empty optional service arrays/objects or unused flags | Wire responses; explicit ownership-based filtering | NOTTESTED |
| C02 | Preserve meaningful false, 0, null | User nested objects and operational status fixture | NOTTESTED |
| C03 | Preserve checkpoint/result/payload keys, values and types | Recursive equality plus source immutability | NOTTESTED |
| C04 | Errors retain code/message and applicable details/outcome/retry | CAS conflict, invalid request, unknown command, message obligation | NOTTESTED |
| M01 | Incoming only; id/sender/text, optional ack/reply | Fresh inbox, pending-only, outgoing-only, duplicates | NOTTESTED |
| M02 | Public names from existing registry | Sender, claims, checkpoints, history, health and recipients | NOTTESTED |
| M03 | Pending semantics proved before changing projection | Read source producer and consumers; coordinator note | NOTTESTED |
| M04 | Serialization never ACKs/replies | Compare isolated message state before/after repeated serialization | NOTTESTED |
| M05 | Messages root-only, no empty placeholders | Run, read, claim and minimal success boundary fixtures | NOTTESTED |
| I01 | Intent warning survives minimal successes | Claim/run/state/update notification fixtures | NOTTESTED |
| I02 | Next write blocked until intent refresh | Existing approved combined-candidate tests; no live probes | NOTTESTED |
| R01 | Run is acceptance-only, actual current status | queued/running/completed/failed/cancelled public fixtures | NOTTESTED |
| R02 | Run adds no wait for start/end/output | Fake scheduler with unfinished work and wait spy | NOTTESTED |
| R03 | Run has only ok/remaining_seconds/cmd_hash/status/queue_id | Exact key equality, queue_id optional | NOTTESTED |
| R04 | Run errors remain unsuccessful | Unknown queue, message obligation and application errors | NOTTESTED |
| D01 | Read keeps command body, stdout/stderr and known exit_code | Multi-page output, exit 0/nonzero, cancel/finish | NOTTESTED |
| D02 | Read retains truncation/retention/continuation | Output loss, retained/truncated and cursor fixtures | NOTTESTED |
| T01 | Detail removes designated fields and duplicate graph | Exact service-field inclusion/exclusion | NOTTESTED |
| T02 | Detail optional collections only nonempty | Each optional field empty and populated | NOTTESTED |
| T03 | Claims retain intent/time and public name | No age or role, no owner/active | NOTTESTED |
| T04 | Snapshot drops detail/status and checkpoint revision | Exact projection and schema validation | NOTTESTED |
| T05 | Latest checkpoint preserves stored type | dict/list/string/JSON-looking string/legacy text | NOTTESTED |
| H01 | Events keep payload and persistent_mutation unchanged | Machine identity fields removed only at event level | NOTTESTED |
| H02 | Comments remove only outer detail/kind | Item equality, including meaningful empty/null values | NOTTESTED |
| H03 | History cursor preserved, no duplicate/lost IDs | Events/comments pages with interleaved insertions | NOTTESTED |
| G01 | Blocking and other relations partition without overlap | Blocker lifecycle and stored graph equality | NOTTESTED |
| G02 | Review rules unchanged despite hidden requirements | Existing review gate tests | NOTTESTED |
| E01 | Compact health exactly six requested fields | Healthy/degraded/failed required components | NOTTESTED |
| E02 | Extended adds full diagnostics, omits auth/identity | Detailed component/terminal/workflow equality | NOTTESTED |
| A01 | Claim ordinary success exactly {ok:true} | Real claim success and public boundary | NOTTESTED |
| A02 | Claim failure retains recovery information | CAS/revision/ownership conflicts, no state mutation | NOTTESTED |
| S01 | Strict schemas reject removed/extra fields | Validate emitted success/failure and negative schema fixtures | NOTTESTED |
| S02 | Descriptions/examples/generated artifacts consistent | Generator --check and targeted doc inspection | NOTTESTED |
| X01 | Intent/Events compatibility on agreed merge base | Read-only combined diff; approved isolated merged tests | NOTTESTED |
| X02 | Neutral Git metadata, published exact SHA | remote SHA equality and repository privacy check | NOTTESTED |
| X03 | No runtime/canonical changes or duplicate full suite | Coordinator ownership receipts and final tree evidence | NOTTESTED |

## Field oracle

`command_run` ordinary success: `ok`, `remaining_seconds`, `cmd_hash`, `status`,
and applicable `queue_id`. Messages and mandatory notices are conditional.
No command/output/coordination/identity/session_lifecycle wrappers.

`task_get(detail)` keeps task identifiers, title/lane/priority/state/revision,
next_action/cooperative/checkpoint/result/tags/isolation_hint/claims,
state_changed_at/created_at/updated_at/description. Nonempty-only fields:
input_refs/output_refs/participants/blocking_dependencies/resource_context/relations.
No owner, active, operational_status, output_state_id, review_requirements,
dependencies, claim_age_seconds or claim role. User-owned values are opaque.

`task_get(snapshot)` keeps task identifiers, title/lane/priority/state/revision,
claim (public name, claimed_at, claim_intent), next_action, description_preview,
description_truncated and latest_checkpoint (checkpoint, author, created_at).
Nonempty blocking_dependencies remain. Legacy checkpoint types must be
preserved based on actual stored provenance; blind JSON parsing is prohibited.

Events keep id/event_type/payload/created_at/agent_name. Comments are unchanged
items. Both lose outer detail and task.kind while request selectors survive.
The existing continuation location must remain usable when present.

Compact health: ok/healthy/application/agent_name/version/storage. Extended:
those plus complete components/terminal/workflow. No auth_mode/identity/status.
Healthy is calculated from all required components before projection.

## Breaking-change inventory to verify

Consumers must migrate from nested command/timer/coordination wrappers to the
new root fields and call command_read for output and exit status. Task consumers
must use public names, typed latest_checkpoint.checkpoint and the two graph
collections; response kind/detail are removed but request selectors remain.
Health clients request extended diagnostics explicitly. Claim consumers fetch
task_get for task details. Refresh connector catalogs after accepted deployment.

## Baseline observations

The independent initial oracle collects 39 cases. Baseline execution: 4 PASS,
35 FAIL (expected old/new contract differences), exit 1. This is a red-control
run, not a candidate acceptance verdict. Examples: old run/claim wrappers,
missing root incoming messages, lost intent metadata, old health fields, and
stringified checkpoints. The four positive controls retain nonempty service
fields. Full logs and JUnit are retained with the independent run artifacts.

`PersistentBackend.message_state` populates both messages arrays from incoming
message obligations. The legacy `AgentCoordinator.message_state` uses formatted
strings in pending_messages and structured records in messages. Neither source
is a sender-delivery receipt stream. Projection must merge structured records
by message ID without acknowledging them. Read/seen bookkeeping in the service
is distinct from serialization and from explicit ACK/reply.

`TaskStore.latest_checkpoint_event` decodes the stored payload_json value, so
its checkpoint already has the original JSON type. `TaskCoordinator` previously
converted it to text (and extracted a dict's text field), losing structure.
The wire oracle exercises the stored value through the public snapshot adapter.
The database row and event history must compare equal before and after reading.

## Oracle v2/v3 correction and extensions

The earlier two raw-application `_meta` cases did not represent the actual intent
injection boundary. They are superseded: the combined implementation observes
intent in the outer role activity wrapper, then adds response metadata and a
separate TextContent after the successful compact projection. The replacement
fixtures exercise that wrapper with an isolated fake mesh. They are explicitly
SKIP/NOTTESTED when the standalone candidate lacks the separately coordinated
intent feature. Their old red-control failures are not implementation blockers.

The v2 control (45 cases) is 8 PASS, 35 FAIL, 2 SKIP, exit 1 on the unchanged
canonical production source. Added positive controls cover invalid arguments
and a real isolated SQLite revision conflict without claim/task mutation.
The deterministic nonwait oracle forbids a status-poll sleep with an in-memory
scheduler. Strict-schema tests first validate the new valid shape, then require
rejection of each removed field; an old schema cannot pass by rejecting both.

The extended oracle has 54 cases, adding all read states, stdout/stderr line
preservation, continuation, meaningful retention/truncation flags and zero
byte counts, required incoming messages, plus the implementer's published
field choice for command body/error (`command.cmd` / `command.error`).

Existing baseline invariant suite independently passed 29 tests (exit 0):
`test_task_history_cursor_stability.py`, `test_task_checkpoint_append_boundary.py`,
and `test_task_revision_side_effects.py`. Cursor is present at task.next_cursor.
Same-stream changes explicitly invalidate its snapshot-bound offset; a restart
recovers the complete stream. This is not evidence of a broken continuation.
