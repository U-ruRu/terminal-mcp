# Subscription-driven Events forwarding

Access number issuance establishes the number's immutable issuer and identity. It does not create an Events subscription or peer interest.

Lifecycle subscriptions use arguments {}. Each Access node advertises one aggregate interest for each subscribed agent.attached, agent.inactive and session.ended type, irrespective of callback count. The source node keeps local attach/activity/session-transition semantics; an inactive event is not task success. Incoming event envelopes do not touch local activity clocks or session state.

The peer wire uses current Mesh peer authentication and the bounded HTTP/retry worker. Trust follows the topology assigned by the paired application and is removed on departure. Event routing does not grant access outside the current namespace. Callback URLs, secrets, credentials, and callback counts stay local.

Lifecycle interest carries a durable increasing version, event_names and a per-type started_at boundary. The boundary starts at the first current subscription, persists through renewals/restarts/outages, and resets after an actual withdrawal/re-add. Explicit withdrawal removes the requested type. Internal leases last 600 seconds and renew every 120 seconds; a 5-second shared worker discovers changes. Expired transport leases pause forwarding, without deleting actual subscriptions or previously queued delivery. Same-interest recovery permits bounded catchup from the original subscription boundary. The receiving Access node independently matches each callback's actual lifetime, so no pre-subscription replay occurs.

Private message.received Events use a separate original-issuer-owned interest for the immutable logical agent and slot. Identity holds do not imply delivery interest. Only active or no-expiry local message subscriptions can announce it. Sources verify the authenticated issuer's known slot/identity, capture node-local recipient Events, and route their envelopes only to that issuer. The receiving issuer rematches its local immutable subscription binding. This channel does not route chat messages or restore the retired generic chat mesh.

Initial private-interest propagation uses bounded backfill of already-authorized node-local message deliveries within 24 hours, preserving their original time. The source message store remains authoritative. No private Event is captured for a recipient without matching local or explicit remote delivery interest.

Both channels preserve source server, public name/slot label, original timestamp and stable source eventId. Imported records are marked with origin_node_id and never re-enter a forwarding outbox. Source outboxes and receiver dedup records survive ordinary restarts/upgrades. A lost acknowledgement retries the same eventId. Network delivery claims recover after 120 seconds if interrupted. Callback authorization is rechecked by the existing local webhook worker.

Retries are bounded to 288 attempts with exponential delay capped at 300 seconds and a 24-hour event horizon. Terminal peer outbox rows are retained within that horizon. Imported Event retention is 24 hours; existing local lifecycle-source retention is unchanged. Individual network batches are limited to 20 Events / 64 KiB. Private delivery-interest snapshots use durable versioned pages of 128 recipients; only a complete snapshot can remove prior interests. Legal large Unicode Events use up to four 32 KiB fragments with digest-checked durable reassembly. Every HTTP frame uses the same canonical UTF-8 JSON 64 KiB budget. Failed delivery does not change WorkSession state or imply task success.

Ordinary restart and upgrade migration preserve subscription identity, callback verification/signing fields, immutable message binding, interest versions and pending delivery. Explicit ttlMs:null support and recipient hold semantics are included from the verified recipient-retention candidate; finite expiry remains separate from identity-hold withdrawal.

QA uses isolated SQLite nodes and ASGI transports with synthetic trust. No production service, trust, credential, network configuration or subscription is modified.

