# Compact public responses

These projections apply to the single current Access, Execution and Coordination
contract in every topology.

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

Public operation responses show the calling agent's incoming native message
obligations, combined by message ID. These are not outgoing delivery status:

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
intact. Continue the existing `message(action="history", detail="summary")` pages
with `next_cursor` to obtain every message ID. Read each complete body with
`message(action="read", message_hash=ID, text_offset=0, text_limit=2048)` and follow
`messages[0].next_text_offset` until absent.

The new by-ID text path returns `text`, `text_offset`, `text_length`, and
`next_text_offset` only when more text remains. Offsets/lengths count Unicode code
points, not UTF-8 bytes or UTF-16 code units. Never increment an offset by byte
length. Concatenation reconstructs the original string and UTF-8 encoding,
including emoji, combining characters, control characters and quotes. An exact-end
offset returns an empty string; a beyond-end offset is an explicit input error.
The per-response text limit is 2048 code points. Final encoded MCP response budget
is preflighted before receipt surfacing. Stored maximum accepted text remains
16384 characters. No stored body is shortened or rewritten.

Native Mesh by-ID reads retain history authorization/retention and existing read
semantics: normal notifications become read; required ACK/reply only become seen.
Per-recipient outgoing delivery tables are omitted from the
text-only slice, and remain available through message history. Chunk arguments
are rejected on ACK, reply, send and cursor-based list operations. Reading another
agent's inaccessible ID returns `message_not_found` without surfacing any receipt.

The old full-history path may still reject a single oversized Unicode body; it
is retained unchanged. The summary-history plus by-ID chunk path replaces it as
the supported bounded recovery route. Native role fixtures verify
exact maximum-length reconstruction, final encoded bytes and unchanged ACK/reply.
Full acceptance is recorded against an immutable source commit.

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

## Compact error notices and committed command recovery

Incoming obligations occur once in root `messages`, including unsuccessful operations.
The error body retains `code`, `message`, applicable non-message `details`, `outcome`
and `retry`. The old `error.details.pending_messages`, aggregate flags and first-message
`message_hash`/`text`/`required_action` aliases are removed from role wire schemas.
Each incoming item retains its message ID, full authority name and required `ack`/`reply`.

Application error normalization bounds the text preview before validating the repair
model. It preserves legal public slot labels and marks shortened text explicitly.
`incoming_messages_truncated` is a workflow warning, not an operation error. Follow the
existing by-ID `text_offset` / `text_limit` read contract to recover the stored original.
The projection does not ACK, reply, rename agents or rewrite stored messages.
Preview limits account for UTF-8 JSON bytes and control-character escaping, not only
character counts. Mandatory metadata remains separate from user-owned objects.

If an accepted command's recovery output exceeds the final serialized response budget,
the failure retains root `cmd_hash` and `error.outcome="committed"`, along with bounded
incoming obligations. Use `command_read` on that receipt; do not run the command again.
An authoritative `unknown` outcome likewise remains `unknown` through normalization.
This guard concerns serialization only; command scheduling and stored output are unchanged.

```json
{"ok":false,"error":{"code":"output_item_too_large","message":"Request a summary or a smaller page.","outcome":"committed","retry":"repair"},"cmd_hash":"aabbccdd"}
```

Refresh Executor/Coordination catalogs after a separately approved deployment. These
changes are an isolated compact candidate; they do not promote canonical or alter the
Events, subscriptions or Mesh release branches.
