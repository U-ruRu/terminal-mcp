# MCP Events message.received — reuse existing Access/attach identity

Status: revised contract and implementation boundary, 2026-10-10.
Supersedes the recipientGrant proposal from 78efa2c8: an additional capability is
unnecessary for a subscriber already bound through the current trusted metadata and
Access Mesh tables. The implementation branch is separate from this document.
Do not claim real dot delivery until a genuine OAuth subscribe/callback succeeds.

## Existing identity mechanism (confirmed in source and disposable HTTP test)

ChatGPT supplies openai/subject and openai/session in the standard MCP request
params._meta. Values are opaque and differ between Access/Executor/Coordinator
connectors, even for one real agent. Terminal MCP already canonicalizes each
pair with ProviderIdentityRegistry into its 64-character binding_key.

AccessMeshApplication.connection_key combines the local node ID, endpoint role,
verified OAuth principal, and provider binding key. This credential-scoped key
is stored by the issuer connection in access_mesh_issuer_bindings and by
Executor/Coordinator attach in access_mesh_attachments. The four-digit
session_number joins those distinct connector identities to the same immutable
issuer_id, slot_id and logical_agent_id. Display names are not identity keys.
Other principals, sessions and node roles cannot borrow a prior pair.

Direct sources:
- adapters/mcp_identity.py: current_provider_evidence; request_ctx exists for tools.
- core/provider_identity.py: OPENAI_IDENTITY_ADAPTER + binding_key.
- application/access_mesh.py: connection_key + attach + issuer_session.
- core/access_mesh_grants.py and storage/access_mesh.py: issuer and attach bindings,
  issuer_bound_slot, attached_slot and canonical number-to-slot resolution.
- storage/access_mesh_messages.py: accepted message/recipient atomic transaction.
- storage/events.py: recipient INSERT generates message.receipt with delivered=1;
  UPDATE read/ACK/reply emits other receipt events without delivered=1.

## Correct subscription resolution (no new recipientGrant)

The custom Events ASGI adapter is not inside FastMCP request_ctx. Therefore it
must validate the same provider metadata from JSON-RPC params._meta itself.
It must also require the already authenticated admission context, the configured
endpoint role and local node; metadata alone never proves authorization.

For an Access-only subscription: resolve the Access issuer connection key through
existing access_mesh_issuer_bindings to an active slot and immutable logical_agent_id.
Empty arguments is acceptable because the protected identity is request metadata,
not an argument. If metadata or issuer binding is missing, fail closed before
callback challenge and do not create a subscription. Store the existing hashed
connection key, immutable slot/issuer/agent, OAuth principal+credential and a
subscription ID with that recipient identity. On every callback, recheck the
active binding and OAuth state; recycled public names are irrelevant.

An Executor/Coordinator attach on the same node uses a distinct connection key
and access_mesh_attachments to the same immutable slot. That existing path
confirms cross-connector identity but does not magically make the issuer's
subscription receive event rows from another physical node.

## Correct message producer and delivery

A local accepted message commits coordination_messages and
coordination_message_recipients, plus access_mesh_messages wire. The insertion
of a recipient obligation, not sender creation, observation, ACK or a receipt
UPDATE, is the business event boundary. Durable source key:
node:message.received:message_hash:immutable_recipient_agent_id.

Create a private record only if there is a matching active recipient subscription
on that node. Callback rows are selected on the same immutable recipient_agent_id.
The user-visible callback data contains server, visible sender, visible recipient,
message_id, original text, and UTC timestamp, with event name in its envelope.
One eventId is stable across bounded attempts; a new reply is a new inbound message
for its actual recipient. Sending a webhook never marks the original message
seen, read, acknowledged or replied. The existing namespace/task delivery checks
were already performed when the recipient obligation was accepted, and the
callback does not widen that recipient set. Private message records should follow
the existing 24-hour mailbox retention.

## Access-only topology: explicit unresolved alternative

**Access per working node.** A local Access subscription can receive messages
accepted on that same node only after its own Access metadata is bound to that
immutable recipient slot. Access on a different node does not automatically
inherit the issuer's identity pair; the current Access MCP tool handles start/end,
while Executor/Coordinator handle attach. For a foreign issuer session on node B,
its Access pair needs an explicitly supported existing attach/binding path before
it can subscribe for that recipient. Do not invent the association.

**One Access aggregation node.** A can authenticate the issuer slot for the agent;
B can independently validate the attached working slot. B-local
coordination_messages / instance_events do not replicate to A. To deliver B events
to the A webhook requires a new bounded, authenticated **event-only** B-to-A relay
using existing Mesh peer trust and immutable issuer/slot IDs. It must authenticate
source node, deduplicate event ID, preserve actual source server in the payload,
match the recipient's existing issuer binding on A, enforce existing scopes and
delivery limits, and not transport/replicate private messages as ordinary mail.
The current implementation has no such relay and cannot claim A received B events.

These two alternatives need an explicit deployment/topology decision; replacing
Coordination Events with Access-only without a relay is not a verified solution
for an agent who works on a remote node.

## Required tests and live acceptance

- Genuine Access issue with one pair, other connector attach with different pair,
  Access events/subscribe with params._meta and arguments={}, node-local incoming
  message to the same immutable logical_agent_id.
- Different agent, different authenticated principal, foreign slot, missing metadata,
  unbound role and stale issuer binding all denied before callback challenge.
- Reused display name still routes only to its originally attached recipient;
  cross-namespace accepted recipients remain isolated.
- ACK, first_seen, read, old receipt updates and webhook retry never duplicate
  event or mutate original receipt statuses; replies get a separate event.
- Preserve HTTPS challenge, callback signature, dual-secret rotation, SSRF
  restrictions, bounded retry and no-retry 410/413.
- Independently QA exact SHA; then coordinated rollout after other blind live
  tests. dot repeats the **real platform subscribe**, and a real local managed
  event and inbound message reach dot with correct server and recipient.
- If ChatGPT does NOT send params._meta on actual events/subscribe, record safe
  ingress/correlation diagnostics and fail closed; only then use a narrowly
  scoped way for Access to supply its existing validated identity context.
  This absence has not yet been verified against real platform traffic.

## Current proof / open limit

The disposable TestClient flow demonstrates extraction of metadata, Access
issuer binding, a different attached Coordinator pair, authorized subscription,
signed callback, stable recipient identity and local message payload. It is
mock callback delivery, not a production subscription in dot.
Cross-node event transport and actual ChatGPT subscribe remain unverified.
