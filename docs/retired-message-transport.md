# Retired cross-node Access Mesh message transport

## Current contract

Access Mesh messages are local to the server that accepted the MCP call. The
current MCP catalogue and role contract describe local send, inbox/history,
recipient lookup, ACK, reply, and alert behavior; `scope="fleet"` remains an
accepted compatibility input and fails with `feature_disabled` before
authorization or storage work. Messages retain their original 24-hour TTL.
Recipient ACK/read/reply state remains local and does not depend on a remote
receipt. `README.md` documents these guarantees.

## Removed implementation paths

`application/access_mesh_messages.py` previously called `flush_for_call` from
the local send path and exposed `flush`, but `flush` was a no-op. The local send
path now calls only local target resolution and `AccessMeshMessageStore.accept`.
The `MeshDelivery` and `MeshReceipt` wire models existed only for that retired
protocol and had no current importers. `handle_peer` remains as the narrow,
authenticated compatibility rejection used by the hidden HTTP endpoint; it
returns `feature_disabled` with `outcome="not_committed"` and does not touch
message state.

`storage/access_mesh_messages.py` no longer provides the old `queue`, `pending`,
`finish_delivery`, `failed_delivery`, or `apply_receipt` operations. Repository
call-site searches found no production callers for these methods: application
code had only the no-op flush, and the remaining references were tests. Receipt
projection now reports only the accepted local recipients and returns empty
`pending_peers`/`delivery_errors` fields for existing output compatibility; it
does not merge historic remote delivery receipts or expose pending jobs.

The private HTTP route
`POST /internal/fleet/access-mesh/messages/{kind}` remains hidden from the
schema and authenticates the configured peer before its compatibility handler
rejects delivery, receipt, and recipient-discovery requests. Keeping this
fail-closed response avoids reviving old peer traffic or turning an authenticated
peer request into a mutation. The separate
`POST /internal/fleet/access-mesh/agents/locations` endpoint remains live: it
serves read-only fleet agent presence to the agent-observation feature, not
message recipient discovery. Its route and `active_agent_locations` have live
application callers and are not part of this retirement.

## Durable data and event behavior retained

No schema, migration, old message row, or old receipt row is deleted by this
change. Startup still calls `activate_local_cutover`, which terminalizes
unacknowledged old outbox jobs with `delivery_stopped_local_cutover`; it does
not set `acked_at`, restore jobs after restart, or inspect malformed historic
message payloads as migration blockers. `prune_expired` still removes only
expired local messages and their subordinate rows. `wire` continues to hide
non-local `:meshmsg:` IDs from inbox/history/ACK and message alert gates.

The similarly named `fleet/message_event_mesh.py` is retained. It forwards
`message.received` lifecycle events to an issuer that has an active event
subscription, based on locally accepted messages and recipient holds. This is
MCP event delivery, not cross-node message delivery or remote message ACK.
Lifecycle event delivery and recipient holds therefore continue unchanged.

No changes were made to the broader Fleet APIs. `/mcp` clients, old slot kinds,
and Console/API compatibility were left intact: Console slot decoders still
accept `legacy` and `persistent` modes (`apps/console/src/api/slotContracts.ts`),
and durable operation-journal parsing still accepts both (`apps/console/src/slots/operationJournal.ts`).
Those are live client contracts, separate from the retired message transport.

## Test ownership handoff

The current test worker owns test edits. Candidate obsolete transport-only
scenarios are:

- `tests/test_access_mesh_messages_v2.py::test_directed_agent_attached_on_both_nodes_gets_each_local_inbox`
  and `::test_remote_only_recipient_is_rejected_before_local_mutation`: these
  assert the old cross-node message-recipient behavior or its transitional
  rejection. Preserve the current local-only rejection contract, but remove the
  obsolete multi-node message-delivery setup.
- `tests/test_access_mesh_messages_v2.py::test_attribution_receipts_and_peer_auth_reject_forgery`
  and `::test_http_router_checks_peer_auth_and_request_size`: replace assumptions
  about peer delivery payload handling with a focused guard that authenticated
  legacy requests are rejected without mutation, if still needed.
- `tests/test_local_messages_24h_victor.py::test_storage_rejects_new_peer_delivery_jobs_before_commit`:
  obsolete once the storage API no longer accepts a peer list; retain the
  application `scope="fleet"` fail-closed test instead.
- `tests/test_local_messages_24h_victor.py::test_legacy_peer_message_request_is_terminally_rejected_without_queue`:
  retain its endpoint rejection and read-only agent-presence assertions; remove
  its pending-queue inspection because that API is retired.
- Any direct calls to `AccessMeshMessageStore.queue`, `.pending`,
  `.finish_delivery`, `.failed_delivery`, or `.apply_receipt` are obsolete test
  coverage of deleted implementation. Keep the tests for cutover terminalization,
  no false ACK/restart replay, local receipt ACK/reply semantics, and TTL.

The new source document records this cleanup rationale; no tests or
`docs/test-scope-cleanup.md` were edited by this implementation worker.
