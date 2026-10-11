# Compact public responses — review candidate

Base: `83677e52660d8c1cc21c10ac0ce59bf6b1fa1b82`.
This changes the public representation only. Canonical promotion and deployment
remain gated by independent acceptance and the coordinator's integration decision.

## Commands

`command_run` commits/submits the command and samples its existing status once.
It never polls for execution or completion and never reads output. For example:

Before (abbreviated):
```json
{"ok":true,"action":"run","command":{"cmd_hash":"aabbccdd","status":"queued","queue_id":1,"exit_code":null},"session_lifecycle":{"state":"active","remaining_seconds":120},"coordination":{"messages":[],"pending_messages":[],"ack_required_pending":false,"alert_pending":false}}
```
After:
```json
{"ok":true,"remaining_seconds":120,"cmd_hash":"aabbccdd","status":"queued","queue_id":1}
```

Call `command_read(cmd_hash=...)` for the stored shell body in `command.cmd`,
current status, known `command.exit_code`, execution error in `command.error`,
and output `lines`. Read retains continuation cursor, counts, truncation and
retention information. Failed and cancelled commands remain readable; read
success describes the read, not the shell exit status. Queue admission does not
invent a new status. Completed status is valid when already observed.

## Incoming notices and minimal mutations

The persistent backend's `messages` and `pending_messages` are aliases of the
calling agent's incoming message obligations. The legacy backend's pending array
contains rendered strings of those same obligations. Neither is outgoing delivery
status. Public operation responses combine structured obligations by message ID:

```json
{"ok":true,"messages":[{"id":"node:msg:example","sender":"Alpha-7 acceptance","text":"Review the candidate","requirements":"reply"}]}
```

Normal notifications omit `requirements`. ACK-only obligations use `ack`;
required textual replies use `reply`. Existing system-alert ACK rules remain.
The operation preview uses the existing four-message / 512-character text limits.
A real truncation produces `warnings` with code `incoming_messages_truncated`.
When entries are omitted, positive `messages_remaining` counts only entries not
displayed in this operation preview; it is not an ACK or delivery-state counter.
No unbounded identifier array is added. Displayed IDs/senders/requirements remain
intact. Continue with the specialized `message(action="history", detail="full")`
operation and its existing `next_cursor`. Stored text and pending obligations
remain unchanged. The command receipt survives even a large incoming batch.

**Existing full-read limit:** a single maximum-length Unicode message can itself
exceed the full-history item/transport budget. Full history then returns
`output_item_too_large`; using `limit=1` does not solve that case. The specialized
message API currently has no text-chunk continuation. This is a separately
confirmed acceptance blocker, not a claim that the preview offers complete
recovery for every accepted message. The preview warning states this limitation;
it must not encourage rerunning an already accepted command.

The projection performs no receipt mutation. Backend delivery/seen/ACK/reply
rules are unchanged. Ordinary `task_claim` succeeds with exactly `{"ok":true}`;
real incoming obligations and warnings are retained when present. Empty warnings
and message placeholders are omitted.

## Task reads

`task_get` keeps request parameters `detail` and `history_kind`; response wrappers
`detail` and history `task.kind` disappear. All task fields stay inside `task`.
Detail removes operational status, output/review metadata, owner/active and the
third dependencies view. Claims contain public name, claimed time and intent.
Optional references, participants, blockers, resource context and relations are
emitted only when nonempty. Only actual blockers are excluded from relations;
the stored graph and review enforcement are untouched.

Snapshot returns `latest_checkpoint.checkpoint`, `author` and `created_at`.
Storage already holds native JSON in checkpoint event payloads and task columns.
The previous snapshot stringification was lossy for objects with a `text` key.
The projection now takes the decoded stored value, preserving objects, arrays,
JSON-looking strings, explicit empty checkpoints, false, zero and null inside
user values. Historical text stays text; there is no heuristic JSON parsing.
Internal compatibility snapshots retain their old preview alongside the value.

Event history removes internal actor/session fields from each envelope while
preserving the entire `payload`, including `persistent_mutation` entries.
Comment history entries are unchanged. `task.next_cursor` continues the selected
bounded history. The cursor is bound to that stream's fingerprint: modification
of that stream returns explicit `invalid_cursor`; unchanged pages neither lose
nor duplicate items. No cursor redesign or stored history rewrite is performed.

## Health

Short responses contain `ok`, `healthy`, `application`, `agent_name`, `version`,
`storage`. Extended responses add complete `components`, `terminal`, `workflow`.
Auth mode and identity diagnostics are absent from both. Hidden mandatory
components still contribute to `healthy`.

## Compatibility and remaining acceptance gates

Clients must refresh Executor/Coordination connector tool schemas after rollout
and migrate command-run field paths, task-get wrappers, incoming message IDs,
checkpoint values, health fields and minimal claim receipts. Refresh alone does
not deploy this source. Access schema refresh is needed with the separately
approved intent integration. Existing request parameters remain supported.

Native Mesh naming is a **blocking unresolved integration issue**: native issue
does not ensure registration with the existing incremental NATO
Access authority and selects an issuer-hash identity. Pre-existing authority rows
may exist; their presence is not guaranteed by the native issuance path. Task projections resolve existing authority names
read-only. They do not allocate names or invent fallback aliases. Native Mesh
health/message-recipient/name acceptance therefore remains HOLD until the
canonical naming source is connected consistently. The coordinator was notified
on the task board because its direct recipient was unavailable.

The held intent candidate `78350547822d6c52ead8538ef971ed81404e8a17` adds warnings
in the outer MCP wrapper (`_meta` plus text), after response projection. That
wrapper is not removed by this candidate. Full combined acceptance is NOTTESTED
until an integration owner and base are approved. No runtime, canonical branch,
or held intent worktree was changed.
