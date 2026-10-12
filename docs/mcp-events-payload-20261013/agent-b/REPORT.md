# MCP Events payload — B: server chain and live-control evidence

Investigation: 2026-10-13 Asia/Tbilisi / 2026-10-12 UTC. Agent B: Quebec-5 on
Secondary, Uniform-156 on BacLOUD. Agent A: Papa-5. Coordinator dot: Romeo-5.

## Status

The acceptance criterion remains open. Four focused tests and an isolated sender-boundary
probe pass. Inspected production source matches the documented ChatGPT MCP Events profile.
A separate user-approved live subscription was created. The second sender did not send,
and the native self-send was rejected with `cannot_message_self` / `not_committed`; therefore
a new end-to-end control is not claimed. Its exact outcome is in LIVE-RESULT.md. No product fix or runtime change has been made by B.

The historical symptom is an absent injected `automations.mcp_event` result. It is not an
observed empty JSON object. HTTP 200 and an automation run do not establish model visibility.
The exact platform stage (acceptance, storage, scheduling, context assembly) is not observable
from Terminal MCP. Publicly matching issue reports are supporting context, not root-cause proof.

## Baseline and isolation

Remote `integration/M3-functional-candidate` and BacLOUD runtime were independently resolved
to `4ecb7201bcc8e2d7ce2b75fcb18d2f77690a66e4` / 0.26.2. The initially selected local bde0dec
was stale; the clean private worktree was fast-forwarded before code tests. Source SHA256 for
`event_server.py`, `event_store.py`, and `event_webhooks.py` matches the installed runtime
byte-for-byte. Exact observations and times: `runtime-observation.json`.

Worktree: `/workspace/terminal-mcp-events-contract-uniform156`.
Branch: `task/mcp-events-payload-quebec5-20261013`. Task: `terminal-mcp/TASK-B800E5`.
BacLOUD commands use B's own Legacy identity. Production SQLite reads use URI `mode=ro`
and `PRAGMA query_only=ON`. Tests use only temporary fixtures. Runtime files, production
subscriptions, dot's sessions, Mesh, policies and slots were not changed by B.
The parallel identity-conflict rollout belongs to Alpha-98; its state is not a payload diagnosis.

## Chain and applicable code

| Stage | Inspected path and evidence | Limit |
|---|---|---|
| Subscription creation | `event_server.py:237+` validates webhook mode, callback and declared arguments; ownership/admission and callback verification precede persistence. The approved test automation and a fresh matching server subscription were observed. | Original platform JSON-RPC request bytes are not retained. |
| Recipient selection | `event_server.py:350+`, message resolver and `mcp_event_message_bindings` pin the admitted recipient. The new subscription is `message.received`, recipient Uniform-156, node BacLOUD, role `session_number`. | Platform automation ID is distinct from server subscription ID. |
| Event generation | `event_store.py:445-531` uses committed local message/receipt records; sender comes from the saved message envelope, recipient from the matching committed delivery, text is copied intact. `event_catalog()` requires six typed `data` fields. | Historical data presence is not delivery into model context. |
| Outbox selection | `event_store.py:1052+` joins event and subscription by their IDs, claims bounded pending deliveries, and rechecks current subscription before send. | A DB batch is not a batched HTTP body. |
| Payload serialization | `event_server.py:165+` creates `{eventId,name,timestamp,data,cursor}` with `data=json.loads(data_json)` and `cursor=null`. `event_webhooks.py:130+` encodes UTF-8 once. | The isolated probe covers this boundary, not live network delivery. |
| HTTP signing/POST | `event_webhooks.py:39+` signs exact `id.timestamp.body` bytes. `_pinned_https_post:86+` posts those bytes with five required headers over validated HTTPS. | Historical exact request headers/body and response body are not saved. |
| HTTP receipt | `event_store.py:1114+` marks `sent` only for 2xx. Read-only observation at 21:18:11 UTC found 32 sent/200 delivery rows. | 200 is acknowledgement only. Response content is discarded by `deliver_due`. |
| Automation/context | User-provided transcripts and current dot observations show notifications promising a following tool result that is absent. The independent test is scoped in LIVE-RESULT.md. | Missing context cannot yield a claimed event count, eventId or `{}`. |

## Contract comparison

Official supported ChatGPT profile:
https://developers.openai.com/plugins/build/mcp-events

One event object per HTTP POST, `Content-Type: application/json`, `webhook-id == eventId`,
Unix-seconds `webhook-timestamp`, Standard Webhooks `webhook-signature`, and
`X-MCP-Subscription-Id == events/subscribe result.id`. The HMAC covers the exact encoded
body; normal event payloads have no JSON-RPC/control wrapper. `data` is an object matching
this server's `payloadSchema`. Complete body is bounded at 256 KiB. ChatGPT may combine
separate deliveries into one run. Sending a JSON array is not the documented batch mechanism.

All inspected properties match. Source/transport checks do not reveal a server-format defect
that justifies changing fields or adding wrappers. No speculative compatibility patch was made.
The public documents do not specify the internal insertion of `automations.mcp_event`.

A's published evidence commit is `bbb7f5cf71b6477ff3fa7fe2115f9fd8191f8367`; its pinned source report is independently reviewed in `PEER-REVIEW.md`. The matching public
incident https://github.com/openai/codex/issues/50714 has an author-provided Node probe at
https://github.com/rokrokss/recly/commit/edb2765d5498c792e43d898946d149ae6ba36303 .
This is a reported dot failure with Work success, not a reproduced success by this investigation.
The Go pending-events fallback is an extra authorized read, not a payload-delivery repair.
OpenAI API webhooks, MCP SSE notifications and cloud-thread notifications are separate mechanisms.

## Focused checks

`focused-tests.txt`: 3 tests passed in 2.48 s and one recipient/message integration test
passed in 6.21 s. One pre-existing Starlette/httpx deprecation warning is recorded.
Tests cover subscription challenge/refresh/unsubscribe, signed delivery and stable eventId,
exact-body Standard Webhooks signature, Access issuance/other connector attach/recipient
subscription/local message delivery. No full acceptance suite ran.

Run the isolated adapter probe from the repository root:

```sh
PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/python \
  docs/mcp-events-payload-20261013/agent-b/transport_probe.py \
  --output /tmp/terminal-mcp-transport-capture.json
```

`transport-capture.json` holds three actual serialized bodies seen by the adapter's injected
sender: one event, then two due events as two distinct calls. Checks include Unicode, quotes,
newline round trip, object nesting, header/body IDs, subscription ID and independent HMAC.
The receiver's 200/empty body is synthetic and explicitly labeled. This file must never be
presented as a captured production HTTP request or a ChatGPT response. The script uses no
network, runtime database or production credential. Its authorization predicate is a fixture;
production authorization remains unchanged and is covered by the existing focused test.

## Evidence needed from the platform

Use the safe IDs/times in LIVE-RESULT.md to inspect callback acceptance and correlate the
resulting automation turn. Confirm the stored event envelope and whether context assembly
created, filtered, truncated or omitted the promised tool-result message. Provide a supported
way to inspect that injected result and a minimal Work-versus-dot control using the same
schema. The unobservable internal boundary must not be presented as a confirmed subcomponent bug.

Closure requires dot's direct observation of the control eventId and sender/recipient/text,
matched after her observation with the sender/outbox evidence. Inbox reads and retries that
only produce 200 do not close this investigation.
