# message.received: public Access session-number routing

The native Access MCP Events catalog declares an optional message.received argument:

    {"session_number":"0123"}

Use the four-character string returned by Access session(action="start"), preserving leading zeros. Subscribe through the original issuing server's Access endpoint using the usual webhook delivery URL and signing secret. Choosing that Access endpoint is the client's responsibility; this patch adds no origin-based request rejection or subscription-transfer mechanism. Attaching an executor or coordinator elsewhere does not change the intended receiving server. The server validates normal authenticated MCP access and terminal:read scope. The number is a public routing identifier, not a credential. Other authorized agents in this server's shared access space may subscribe to the same number with their own verified callbacks.

## Recipient and fan-out

The server reads the existing Access Mesh number winner and slot. A new subscription or refresh requires a live Access cycle and an active slot. It does not attach a connector, create an agent, issue credentials, or change a work session. Each callback is bound to that exact LogicalAgent, issuer, and slot. A message to that recipient is delivered to every valid callback subscribed for it. Messages for other recipients are excluded.

Explicit session_number selects the recipient even if the subscriber has different provider subject/session metadata. Provider metadata is not a second recipient selector. No raw provider identity is retained or exposed. Existing arguments:{} subscriptions remain supported when trusted request _meta resolves their existing Access binding. All other event argument schemas and message payloads are unchanged.

## Lifecycle and number reuse

An existing callback association survives ordinary Access end/start for the same number and slot, until its ordinary webhook TTL expires, the owner unsubscribes, its authentication is revoked, or the slot is suspended/deleted. WorkSession IDs and epochs do not pin this association. Existing valid queued messages may finish delivery after end; the ordinary local messaging gate prevents new sends to inactive recipients. New subscriptions and refreshes are rejected while the target cycle is ended or expired.

A released or recycled number must not silently redirect an old callback to another LogicalAgent or slot. Delivery rechecks the number winner against the stored immutable recipient. A changed mapping invalidates old deliveries; the old association remains pinned until TTL or explicit unsubscribe, so automatic delivery cleanup cannot let a refresh silently redirect it. When the owner tries to refresh an active tuple after reuse, the server returns recipient_binding_changed and requires unsubscribe followed by a new subscribe. Once a subscription has expired or been removed, a fresh subscribe selects the then-current mapping.

Unsubscribe uses the authenticated callback owner's exact event name, arguments, and callback URL, without requiring a current recipient binding. It remains idempotent after end, expiry, or number reuse. A different number or callback owner is unaffected.

## Contract checks

Run scripts/build_event_catalog.py to regenerate docs/contracts/mcp-events-list.json and run it with --check to verify it. This native Events schema is separate from tools/list compact catalogs. Callback challenge verification, signing, secret rotation, retry limits, and OAuth revocation checks use the existing implementation unchanged.

All new behavior is covered by isolated fixture tests. This candidate does not configure live callbacks or modify a deployed server.

## Human agent names

Mesh presentation uses the existing NATO alphabet and cycle arithmetic. The
negotiated public four-digit number maps to ordinal int(number) + 1; the short
name always includes its cycle, including -1. For example, 0000 is Alpha-1 and
0026 is Alpha-2. Leading zeros remain unchanged in session-number routing.

Issuance retains the short name and any existing slot label as display metadata.
A present label is appended with the separator " - "; no label is invented.
The same full name appears in event agent_name, agent overview display_name and
public_name, and message recipient records. Both the short and full names resolve
to the same immutable recipient. Internal issuer-qualified routing aliases remain
accepted, while successful message records retain the resolved human label.
An ambiguous human target is rejected rather than sent to an arbitrary slot.

Ordinary end/start retains the assigned name and logical identity. Reused numbers
can repeat a human name, so callback binding and message replies continue using
immutable identities. Lifecycle events preserve the attachment name snapshot;
message events preserve delivery-time sender and recipient labels. A slot with
neither recorded name metadata nor a retained public-number mapping reports
agent_name_unavailable instead of presenting a hash as a human name.

The new issuance metadata changes the Mesh wire event shape. All active peers
must run the compatible parser before new issuance metadata is exchanged;
mixed-version rollout is not supported by this change. Source server fields,
credentials, grants, provider bindings, and access generation remain unchanged.


## Durable subscribed recipient identity

A message.received subscription pins its exact recipient LogicalAgent, issuer,
slot and four-digit number. This applies to public-number arguments and existing
provider-bound message subscriptions. Lifecycle subscriptions with empty
arguments do not pin any identity.

A held recipient remains discoverable and addressable on the subscription node
after its WorkSession ends. Discovery reports inactive message-only presence,
with zero work time. Sending a message does not start a WorkSession, renew its
deadline or grant command/write access. Slot deletion, code rotation and known
number reuse are rejected while any matching hold exists.

Each subscription owns one durable hold. Owner-authorized exact-tuple unsubscribe
deletes the subscription, binding and queued deliveries, and commits its hold
withdrawal in one SQLite transaction. Removing one subscription preserves all
other holds. Removing the last hold does not bypass a live grant, session,
reservation or any other existing number constraint. Credential revocation or a
callback 410/413 stops delivery without pretending that the owner unsubscribed.

### Expiration and persistence

The official MCP Events contract distinguishes omitted ttlMs (server default),
explicit null (requested no expiration), and a finite requested duration.
This server grants explicit null with refreshBefore:null. Omission preserves the
one-hour default. Finite requests preserve the existing one-hour maximum and
return the actual granted ISO expiration in refreshBefore; delivery stops at
that expiration. Identity holds remain until explicit removal, including when a
finite delivery lease expires. Use no-expiry for persistent monitoring.

Upgrades make expires_at nullable by copying all existing subscription fields
without changing callback, signing/rotation configuration, recipient association,
created_at or expiration. Existing finite leases are not silently converted to
no-expiry. Durable bindings backfill missing holds, and ordinary restart recovers
queued delivery without requiring an agent to subscribe again. No deployment
step should reset these SQLite tables or renew a lease.

Only a hold's owning subscription node can advance its authenticated Mesh
snapshot. Revisioned release tombstones reject delayed older snapshots; callbacks,
secrets, credentials and message bodies are never part of this hold transport.
Known replicated holds survive peer outage and reject local number reuse.
They also preserve message-only addressability on peers with an already existing
exact local attachment. A replicated grant without a local attachment does not
create message presence on a new node.

### Strict allocation during a partition

New number allocation requires every configured trusted Mesh peer to confirm a
reservation for the same number. This includes numbers never seen by the local
node: local absence cannot prove that an unreachable peer has no existing hold.
A missing, failed, timed-out or mismatched reply returns the retryable
session_number_unconfirmed error and creates no slot or number claim. No
timeout fallback issues an unconfirmed number. The existing 30-second total
negotiation bound remains, including best-effort cleanup; peers that cannot be
reached retain only the existing short-lived reservation lease.

This deliberately trades new registration availability for held-number safety.
Existing sessions, command/write deadlines, message subscriptions and normal
peer reconciliation are unchanged. With no configured peers, local reservation
is sufficient. Explicitly selected numbers use the same proof when replication
is configured, and a conflict does not silently substitute another number.

After owner unsubscribe, the atomic local hold withdrawal and revisioned peer
tombstone remove only subscription occupancy. Every peer must still accept the
number under its remaining live-session/grant/reservation rules before it can
be reassigned. During reconnect/catch-up a peer may conservatively keep rejecting
until its release tombstone arrives; no old subscription is retargeted.

No existing subscription lease is restarted, no historical number attribution
is deleted, and no network/security setting is changed by this policy update.

Unsubscribe suppresses queued deliveries and already-claimed deliveries whose
subscription was removed before dispatch. An HTTP callback request already in
progress cannot be recalled. At-least-once retry behavior remains unchanged.

Official contract: https://developers.openai.com/plugins/build/mcp-events
