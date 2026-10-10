# MCP Events message.received — immutable recipient authorization contract

Status: fail closed, specification only, 2026-10-10. This document does not enable
message.received in the production catalog. Real delivery is blocked until the
recipient-binding mechanism passes independent security acceptance.

## Confirmed identity gap

The live dot Coordination OAuth events/subscribe call carries arguments={} and an
OAuth client subject. That identifies the client, not the immutable LogicalAgent
recipient. Public legacy session names can be reused and are not authorization
principals. An OAuth client alone cannot select its own recipient or receive the
private text of all messages.

Native tables: coordination_messages holds message_hash, sender_agent_id,
text, created_at, target_name and task namespace; coordination_message_recipients
holds immutable recipient_agent_id and has PRIMARY KEY(message_hash,recipient_agent_id).
AccessMeshMessageStore.accept transactionally creates both. Read/ACK/reply receipts
update old rows. Only INSERT of the exact recipient obligation is a new delivery.
The message.created journal event alone cannot authorize arbitrary subscribers.

## Minimal safe implementation

1. On the recipient node, the existing authenticated managed-session/AccessMesh
   attach tool must verify the current OAuth principal AND credential, provider
   identity evidence, exact logical_agent_id, work_session_id, session_epoch,
   node_id, grants, and active session, never a public display name.
2. Add a recipient-notifications enrollment action reachable only through that
   already-verified agent-bound tool context. Issue a 256-bit opaque single-use
   recipientGrant capability. Store only its digest and exact immutable recipient,
   node, session, epoch, principal, credential, auth generation, namespace scope,
   issued-at, expires-at (at most 10 minutes to redemption), used/revoked status.
   Do not log or expose the raw grant.
3. Advertise message.received through events/list ONLY after that implementation is
   complete. Its inputSchema must require recipientGrant. events/subscribe
   atomically redeems the proof and checks the same verified OAuth principal AND
   credential. Never accept free-form recipient, public name or arguments={}
   as authorization. Persist recipient binding, callback and subscription expiry.
4. For each queued callback, recheck active OAuth access and immutable recipient
   session/epoch, node, auth generation and namespace scope. Revocation, session
   replacement, or unsupported principal identity stops delivery fail closed.
   A new session with the same public name is not the former recipient.
5. After a committed local message delivery, enqueue one event for each authorized
   recipient from coordination_message_recipients INSERT, keyed durably by
   node:message.received:message_hash:recipient_agent_id. Retry keeps same eventId;
   read, ACK and receipt UPDATE do not create an event. A new reply is a separate
   message and may be delivered only to its actual recipient.
6. User data requires server, visible sender, visible recipient, message_id,
   original text and UTC timestamp. Keep raw grant/credentials/private LogicalAgent
   IDs, other recipients, and unrelated namespaces outside the callback. Do not
   alter message seen/read/ACK/replied fields to send a webhook. Respect 256 KiB
   signed callback limit and existing callback challenge, SSRF, HTTPS, bounded retry,
   410/413 no-retry, unsubscribe and five-minute dual signature rules.

## Acceptance

- OAuth client without recipientGrant: denied and no delivery rows.
- Reused public legacy name, guessed recipient, foreign node grant, wrong principal
  or credential, stale session epoch, revoked grant, and namespace mismatch: denied.
- FirstByte grant alone never reveals BacLOUD mailbox; local attach must be proven.
- Distinct messages and replies are delivered once per immutable recipient; duplicate
  receipt INSERT, retry, ACK/read and metadata UPDATE do not duplicate the event.
- Signed challenge succeeds through actual dot connector and real message body
  reaches only the authorized recipient. A mock sender and event catalog are not
  proof of this end-to-end requirement.

## Blocking fact and current release boundary

The dot platform currently creates OAuth Coordination subscriptions with arguments={}
and no recipient binding. Terminal MCP cannot prove which immutable agent owns that
subscription. message.received must remain unadvertised and delivery disabled.
The separately committed 3-event MCP Events lifecycle implementation may proceed to
QA/release approval independently, without leaking message content.
