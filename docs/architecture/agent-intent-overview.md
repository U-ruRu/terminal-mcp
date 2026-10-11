# Local agent overview and call-boundary intent freshness

## Scope and compatibility

Coordinator `agent_observe` is an authoritative view of this execution server's
existing attached slots and latest local WorkSession. It is not a fleet view.
The result declares `scope: "local"` and `server_id`. Call the Coordinator on
each server for that server's state. No new Mesh transport or identity registry
is introduced. OAuth, Access issuance, grants and recipient authorization are
unchanged.

The existing `public_name`, `last_server`, `session_duration` and `last_activity`
fields remain. The composition deliberately changes from active message-location
records to local durable session records. `message(action="recipients")` remains
the address book; it is not the overview and is not used to infer intent.

Default `limit` is 20, maximum 100. Continue `next_cursor` unchanged. The cursor
is bound to `include_inactive` and `logical_agent_id` filters. Default results
include live sessions; `include_inactive=true` adds the latest ended or expired
session for each attached slot. This is not an unbounded session-history API.
`logical_agent_id` resolves the stable agent ID supplied by existing Events.
Events payloads and message addressing are unchanged.

The overview adds the actual native `logical_agents.display_name`, stable
`logical_agent_id`, server, current intent, `intent_updated_at`, `intent_state`,
`activity_stale`, session ID/epoch, session state/start and expiry. If no readable
operator name exists, the existing public-name fallback is shown. Intent is
never synthesized from commands or names. Empty intent is `missing`, and ended
records retain their last declared intent. Intent age, activity age and session
expiry are independent. Activity staleness means no recent observed successful
call; it does not prove that an already-running command has stopped.

## Starting and updating intent

Access `session(action="start")` issues a session number; it is not a declaration
of work intent. Executor/Coordinator `session(action="attach", session_number=...)`
keeps the old attach receipt and accepts optional initial `intent`. Before the
first domain write, declare an intent, either on initial attach or with
`session(action="intent", intent="Review the selected task")`. Refresh uses the
existing connector binding and forbids resubmitting a session number.

Intent must contain non-whitespace text and have at most 1000 characters.
Repeated attach preserves both existing intent and its timestamp. Only the
explicit intent action refreshes an existing declaration. Refresh is intentionally
not advertised as idempotent: accepting the same text later advances its timestamp.
It does not renew an Access cycle, create credentials or alter session expiry.
Intent is shared by the two roles of the same local WorkSession, not by remote
servers. Each new local WorkSession starts with missing intent.

## Deterministic enforcement

The public Access Mesh V2 Executor/Coordinator MCP boundary observes intent
before request validation and before any domain mutation. Initial missing
intent immediately rejects a domain write with `intent_required`. Read and safe
cleanup paths remain available under their original authorization/lifecycle rules.

Freshness expires at exactly `intent_updated_at + 180 seconds` (`now >= due`).
The first observed call at or after this instant atomically persists a warning
and receives grace: its ordinary validation and execution are unchanged. This
also applies to reads and calls that fail validation or another domain gate.
The response contains an `intent_warning` in MCP `_meta` and an additional text
content item: update intent or the next write will be blocked. The first content
item remains the canonical JSON copy of `structuredContent`; strict business
payloads do not acquire an unadvertised field.

Every later domain write without accepted refresh is rejected with `intent_stale`
and `outcome="not_committed"` before mutation. Reads/status, intent update,
command cancel/recovery, claim release and message ACK/reply bypass the intent
block only; existing permission, ownership, alert and session gates still apply.
Access end remains available. A successful refresh clears the warning. An invalid
refresh does not. Expired/revoked cycles retain their original lifecycle rejection
rather than gaining access through intent refresh.

SQLite `BEGIN IMMEDIATE` serializes warning admission and refresh. Concurrent
calls receive exactly one first-call grace. The warning is durable across restart
and reset when a new local WorkSession replaces the old mapping. Migration adds
three nullable columns to the existing local-session table; no new registry is
created. Old live sessions therefore require an initial declaration before writing.

No background call or heartbeat is forced. Reading an existing long-running
command neither refreshes intent nor starts another command. Reconcile its
`cmd_hash` before considering a retry. A blocked retry does not erase prior
receipts or imply that an earlier ambiguous command did not execute. Existing
legacy coordinate/task-lease behavior is preserved outside the V2 MCP boundary.

## Verification

Focused regression coverage includes initial intent, the exact 180-second
boundary, first-write grace, zero-effect later rejection, accepted/invalid refresh,
failed-read warnings, safe recovery, concurrent admission, restart persistence,
repeat-attach semantics, long-command read/cancel without duplicate execution,
local stable IDs/names, timestamps, ended filtering, bounded pagination and strict
output-schema validation. Effective role schemas and compact catalogs are
regenerated from the source definitions.
