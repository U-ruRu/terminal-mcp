# MCP Events payload — independent follow-up review

Date: 2026-10-13. Baseline: `b6b0605d893444ccce50089c6a865891fb63d8f8`.
This is the independent reviewer report. The primary reviewer owns the sender harness
in the adjacent `agent-a-followup` directory (published peer candidate
`1dcd7cd114f9dc4cdbae804eaa12d00c5c7a6d12`). Operator-specific records are intentionally
kept outside Git; this directory contains environment-neutral evidence only.

## Result

The current native sender and the documented OpenAI sender produced **identical event
body bytes and the five required application headers** in all three differential cases
at a real isolated TLS receiver. A separate independent run completed six HTTPS requests:
three through the current native adapter/serializer/signature/HTTPS implementation and
three through the hash-pinned OpenAI JavaScript `sendEvent` function.

This closes a gap in the earlier source-only / injected-sender tests. It does not prove
that any current callback payload reached a ChatGPT model. The acceptance criterion is
still open. A new native Work-versus-dot comparison is being coordinated; no synthetic
local response is presented as a platform receipt.

## What was actually executed

`independent-https-rerun.json` records the independent run. `independent-review.json`
records the exact reviewed harness snapshots. Before execution, 28 public source files
were matched to their source-manifest SHA256 values. Only reviewed sender/signature
functions ran; no external application bootstrap, package lifecycle script or live gateway
was started by the independent reviewer.

The native subject is the unchanged `MCPEventsAdapter.deliver_due` path, including
JSON serialization, Standard Webhooks signing and the real HTTPS POST implementation.
The peer harness uses a temporary TLS certificate and a loopback HTTP receiver. Test-only
DNS/socket facades map one synthetic address to that listener. Certificate and hostname
verification remain enabled. The store and authorization predicate are synthetic fixtures;
no production authority is bypassed or reconfigured. Public DNS/egress are outside this test.

| Case | Observed result |
|---|---|
| One ASCII message event | Native and reference bodies/headers identical; signatures verified. |
| Unicode, quotes, newline and emoji | UTF-8 bytes preserved; identical bodies/headers. |
| Two due events | Two distinct requests, each containing one object; no array batch body. |

The compared headers are `Content-Type`, `webhook-id`, `webhook-timestamp`,
`webhook-signature` and `X-MCP-Subscription-Id`; `Content-Length` also matched.
The native fixture had `Accept-Encoding: identity`; the reference fixture's HTTPS
adapter added `Connection: close`. These are observed transport differences, not extra
MCP event fields. The test does not establish their behavior on every platform receiver.

Hookdeck's selected `signStandardWebhook` function and Recly's selected `signedHeaders`
functions also matched `standardwebhooks` 1.1.1 for each exact body. This is a signature
comparison, not an end-to-end execution of those external services.

## Concrete public implementations and the real differences

### Official OpenAI example

https://developers.openai.com/plugins/build/mcp-events

The inspected documentation snapshot has SHA256
`68c534c9d1bd3f9322b31b515f10d56d69cbcefb6c6be1e314c116b2a68e0632`.
It documents Work/dot support and the event object `eventId`, `name`, `timestamp`,
`data`, `cursor`; callback acceptance and asynchronous model processing are separate.
The exact selected `sendEvent` source hash is in the TLS receipt. The reference uses
JavaScript and a Standard Webhooks library; the native sender uses Python standard-library
HMAC. The actual signed results matched, so a language/library rewrite has no demonstrated
benefit for the observed missing-context symptom.

### Hookdeck bridge

https://github.com/hookdeck/mcp-events-bridge/tree/73483a0849349596db5b5f2feb2ba96269684cb6

This bridge uses an external event gateway for provider ingestion, routing, persistence
and retries. Its documented ChatGPT setup demonstrates a Work-oriented integration.
Its outgoing event envelope and required signing headers match the native format.
Its gateway architecture and provider credentials differ from local durable outbox and
session-bound recipient routing. Gateway reliability is not proof of dot context insertion.
The external gateway was not executed by this review.

### Recly probe

https://github.com/rokrokss/recly/commit/edb2765d5498c792e43d898946d149ae6ba36303
https://github.com/openai/codex/issues/50714

The author reports success in Work and missing `automations.mcp_event` in dot with the
same backend. That is an author observation, not a new reproduction by this reviewer.
The selected sender signs the same raw bytes and application headers. The public issue
does not identify a demonstrated alternative body format that fixes the symptom.

### Python MCP webhook-events and Feishu-dot bridge

https://github.com/s1980amber-commits/mcp-webhook-events/tree/2a5a00540ecb96509752537a0a4de575f0b3d183
https://github.com/yaodiff/feishu-dot-bridge/tree/ae337fb1dc70dff4e355bf785418cab712f12031

The Python package explicitly offers `get_recent_events` to recover event information
when a dot wake omits it. Feishu's `docs/OWNED_EVENT_READS.md` defines `list_pending_events`
and `get_event` for the same recovery need. Both require their own scope/authorization
checks. This is the material operational difference: wake followed by an authorized read
can keep a workflow running while the raw injected webhook payload is still absent.
That recovery design is not evidence that a different webhook format solved injection.
No such fallback was substituted for the required payload-only acceptance test here.

### Additional vendor example: Upstash

https://upstash.com/docs/redis/sdks/agentkit/mcp-events
https://upstash.com/blog/mcp-tasks-and-events

The vendor documents Redis subscriptions and QStash delivery to a server endpoint which
then signs and forwards the same singleton envelope to the host. It reports an end-to-end
ChatGPT monitor test. This backend was not executed here and the report does not establish
raw payload visibility in dot. One library-specific difference is that input-filter fields
must also occur in payload data. That is not a general MCP requirement and is not a reason
to copy a session-access number into this server's message payload. The native recipient
resolver uses the number for binding; delivered data contains sender/recipient names.

## Live evidence boundaries

The inspected runtime is version 0.26.3 on the baseline SHA. Its three event implementation
files match the earlier examined baseline byte-for-byte. A read-only metadata snapshot
found four original subscriptions and 57 aggregate HTTP-200 delivery records; no new separate
test subscription was present at that observation. These counts are not assigned to a
specific control or claimed as evidence of model visibility. No raw historical wire body
or signature was reconstructed and presented as a capture.

A prior controlled native singleton has a saved receiver statement reporting the entire
injected payload absent before recovery reads. Separately, an older Work conversation
contains a receiver statement reporting a visible lifecycle payload and specific fields.
The original raw tool-result block from that older positive report was not recovered.
Therefore it is a historical positive lead, not a current independently verified success.
The current runtime versus that older runtime was not treated as identical without evidence.

The coordinator launched a test agent, but its initial environment was Codex and lacked
MCP event-subscription/context-delivery capability. It created neither a session nor a
control event. This is **setup unavailable**, not a failed payload delivery. A genuine Work
control was requested separately. A later coordinator message confirmed a real Work chat
with event-discovery and webhook-creation capability; its destination was still being
resolved. Receiver/subscription readiness and a new model observation are still required
before a new result can be asserted.

The coordinator explicitly prohibited SSH and transferring callback secrets. The proposed
reference-sender live arm was not executed. Original subscriptions were left unchanged.
A previously canceled dedicated Work test was identified as a possible native recovery
route, but was not reactivated. The coordinator explicitly chose a fresh Work test and instructed
that the previously canceled task remain unchanged.

## Diagnostic helper review

The review found an ambiguity risk in the prepared offline reference CLI: writing an
exclusive receipt only after POST could fail after a successful send. The primary reviewer
changed it to reserve the receipt before network activity, record attempt phases durably,
and distinguish `not_attempted`, uncertain delivery and an incomplete receipt after a
response. Automatic retries remain disabled. Null cursor and timezone-bearing timestamp
validation were added. This improves the diagnostic helper; it does not change product code
or fix the original payload symptom.

`reference-cli-independent-review.json` records four independently executed negative cases:
existing capture file, private callback address, unsupported cursor, timestamp without a
timezone. All returned `not_attempted`; no live callback was sent. `node --check` also passed.
Approval is limited to the offline diagnostic scope; a future live run still requires its own
authorization and current test subscription.

## Next discriminating experiment

Use a real Work receiver and a distinct currently attached native sender. Create or
coordinate one isolated test subscription. Send one new native control with a nonce that
has not been disclosed to the receiving model. The receiver first records the actual
injected tool result or its complete absence, without inbox/history/recovery reads.
Only then correlate the sender receipt, event ID and payload. Repeat on dot with the same
schema, preserving the distinction between the platform host and the sender implementation.
Disable only the temporary agreed subscription after the bounded test. No field guessing,
SSRF exception, authorization removal, runtime restart or batch-array replacement is needed.

No product correction or deployment is included in this follow-up. Precise internal platform
loss attribution still needs a correlated platform trace or a completed comparative live run.

## Cross-review and publication

The primary reviewer independently read this report and the three accompanying JSON
receipts, recomputed all six captured-body hashes, and checked the event/header IDs and
six message-data fields. The peer verdict and exact evidence hashes are preserved in
`agent-a-followup/peer-review.json` at peer commit
`1dcd7cd114f9dc4cdbae804eaa12d00c5c7a6d12`. Both reviewers distinguish real local TLS
acceptance from payload visibility in a live model. The later vendor-reference paragraph
was also directly checked against its primary documentation by this reviewer.

## Later transport-default cross-check

The primary reviewer added one short test of the reviewed Upstash `signWebhook` and
`postSigned` function bodies with real Node `fetch` and a loopback TLS receiver, at peer
commit `90d574e658b70ffd0335a6d2500d10c15b38d748`. This reviewer read the harness and
receipt and recomputed its captured-body digest, size, identifier correspondence and six
data fields. This later execution was not repeated by the independent reviewer.

Unlike the original safe HTTPS adapter, real Node fetch added `User-Agent: node`,
`Accept: */*`, `Accept-Language: *`, `Sec-Fetch-Mode: cors`,
`Accept-Encoding: br, gzip, deflate` and `Connection: keep-alive`.
The event body and required application headers/signature remained correct. This limits
any claim that only two transport headers differ: that was true of the first two selected
transport adapters, not of all public clients. Causal influence on the platform is still
unmeasured. No production header patch was made and no live callback was attempted.
The review receipt is `upstash-fetch-review.json`.
