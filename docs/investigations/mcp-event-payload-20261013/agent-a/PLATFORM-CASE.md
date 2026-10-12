# Platform escalation draft: MCP Events trigger without model-visible payload

Status: investigation package, not a confirmed platform subcomponent diagnosis.
The fresh blind-control outcome must be attached before presenting this as a newly
completed reproduction. No platform issue or support ticket was submitted by this agent.

## Symptom and expected outcome

An MCP Events callback starts a dot automation. The run notification refers to a
following `automations.mcp_event` tool result. That result is absent from the model-visible
context; the method is also absent from the callable tool catalog. This is an absent
message, not an observed empty JSON object.

Expected: the model can read the actual event's `eventId`, `name`, `timestamp`, and `data`.
For Terminal MCP `message.received`, data has `server`, `sender`, `recipient`,
`message_id`, `text`, `timestamp`. The observed eventId and test text must match the
sender's recorded event, without an inbox/read fallback or an advance coordination hint.

User-provided singleton notification:

```text
Use the event below as the new information to evaluate for this run.
Webhook family: mcp_server/event
Webhook event:
Event data is supplied in the following automations.mcp_event tool result.
```

The notification ends there. A user-provided grouped notification also refers to a
following tool result that is absent. The number and contents of omitted events cannot
be recovered from the statement that multiple events triggered the run.

## Confirmed local baseline

Terminal MCP BacLOUD 0.26.2, source
`4ecb7201bcc8e2d7ce2b75fcb18d2f77690a66e4` was inspected at 2026-10-12T21:17:36Z.
The three installed event files match the source checkout. At 21:18:11 UTC the event
store contained message payload objects with the expected six fields and 32 delivery
rows marked `sent`, HTTP200. These are server-side observations, not proof of model data.

The production schema retains HTTP status, not exact HTTP request/response bodies.
The included serializer captures use a synthetic receiver and must not be described
as captured production traffic. They demonstrate one serialized event per sender call,
correct object nesting, UTF-8 contents, byte digests, subscription/event header IDs,
and exact-body signing. An independent published Standard Webhooks vector passes for
the two project signer functions.

Evidence index: REPORT.md, CROSS-REVIEW.md, signature-vector-result.json,
agent-b-boundary-review.json, sources.json. B's own package contains the runtime
observations, focused test receipts, sender-boundary probe, and live-control status.

## Fresh test correlation

Dot coordinator: Romeo-5. B's agreed test recipient: Uniform-156 on BacLOUD.
Automation ID (reported by dot to B): `6acd4f27939c819186c3ea71c2970103`.
Server subscription ID (verified in saved primary command output):
`sub_3135371242a9cae4c8b0937b9229b56de003bc4f`.
Created `2026-10-12T21:20:41.153Z`; expiry `2026-10-12T22:20:41.153Z`.
Existing dot subscriptions remain separate and unchanged.

At the last recorded pre-control status (2026-10-12T21:25:29.396Z), no control event
had yet been sent. Therefore control eventId, HTTP receipt, automation turn ID and
blind-context outcome are not yet asserted here. The values belong in a dated
LIVE-RESULT supplement after observation; they must not be invented from other runs.

## Minimal reproduction procedure

Use a scoped, authorized MCP Events subscription created by the platform, with
its supplied callback and secret retained privately by the server. Use the normal
Terminal MCP message API and an agreed test recipient; preserve real trust and
recipient checks. Send one harmless message with a new nonce. Dot must report the
webhook fields or `payload_absent` before looking up any recent messages. Compare the
independent reply with server evidence for that same eventId. A Work-versus-dot
comparison using equivalent independently subscribed callbacks is a useful additional
control, not a replacement for the dot acceptance criterion.

The already published matching minimal Node server can be inspected at:
https://github.com/rokrokss/recly/commit/edb2765d5498c792e43d898946d149ae6ba36303

Its author's related report:
https://github.com/openai/codex/issues/50714

The report is open with no comments at inspection. Its author describes a Work success
and a dot failure. We inspected its code; we did not independently rerun its live test.
Do not run its unauthenticated modes or copy its diagnostic logging into production.

## Questions requiring platform-side evidence

For a completed control's eventId/subscriptionId, establish whether the full envelope
was accepted and persisted, associated with the intended automation, and carried into
context assembly. Then establish whether the promised tool-result message was created,
filtered, truncated, omitted, or made inaccessible at another layer. Public MCP Events
transport documentation does not expose these internal stages or a callable retrieval
contract for `automations.mcp_event`.

Return a supported resolution and verify a new singleton payload in dot. Test grouped
runs only after the singleton result, with separate event POSTs. Do not treat an HTTP
acknowledgement, a scheduler wake, or an authorized recovery read as a payload-delivery fix.

## Redaction policy

Never publish the full callback URL, callback path/query tokens, signing secrets,
authorization credentials, session access codes, or actual signature values. Preserve
safe correlation IDs, UTC times, status codes, body type/size and hashes. Synthetic test
message content can be shared after the blind observation. Private response metadata
must be reviewed before being attached to a public issue.
