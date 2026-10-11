# message.received: public Access session-number routing

The native Access MCP Events catalog declares an optional message.received argument:

    {"session_number":"0123"}

Use the four-character string returned by Access session(action="start"), preserving leading zeros. Subscribe through the Access endpoint using the usual webhook delivery URL and signing secret. The server validates normal authenticated MCP access and terminal:read scope. The number is a public routing identifier, not a credential. Other authorized agents in this server's shared access space may subscribe to the same number with their own verified callbacks.

## Recipient and fan-out

The server reads the existing Access Mesh number winner and slot. A new subscription or refresh requires a live Access cycle and an active slot. It does not attach a connector, create an agent, issue credentials, or change a work session. Each callback is bound to that exact LogicalAgent, issuer, and slot. A message to that recipient is delivered to every valid callback subscribed for it. Messages for other recipients are excluded.

Explicit session_number selects the recipient even if the subscriber has different provider subject/session metadata. Provider metadata is not a second recipient selector. No raw provider identity is retained or exposed. Existing arguments:{} subscriptions remain supported when trusted request _meta resolves their existing Access binding. All other event argument schemas and message payloads are unchanged.

## Lifecycle and number reuse

An existing callback association survives ordinary Access end/start for the same number and slot, until its ordinary webhook TTL expires, the owner unsubscribes, its authentication is revoked, or the slot is suspended/deleted. WorkSession IDs and epochs do not pin this association. Existing valid queued messages may finish delivery after end; the ordinary local messaging gate prevents new sends to inactive recipients. New subscriptions and refreshes are rejected while the target cycle is ended or expired.

A released or recycled number must not silently redirect an old callback to another LogicalAgent or slot. Delivery rechecks the number winner against the stored immutable recipient. A changed mapping invalidates old deliveries. If an active subscription tuple still exists when the owner tries to refresh it after reuse, the server returns recipient_binding_changed and requires unsubscribe followed by a new subscribe. Once a subscription has expired or been removed, a fresh subscribe selects the then-current mapping.

Unsubscribe uses the authenticated callback owner's exact event name, arguments, and callback URL, without requiring a current recipient binding. It remains idempotent after end, expiry, or number reuse. A different number or callback owner is unaffected.

## Contract checks

Run scripts/build_event_catalog.py to regenerate docs/contracts/mcp-events-list.json and run it with --check to verify it. This native Events schema is separate from tools/list compact catalogs. Callback challenge verification, signing, secret rotation, retry limits, and OAuth revocation checks use the existing implementation unchanged.

All new behavior is covered by isolated fixture tests. This candidate does not configure live callbacks or modify a deployed server.
