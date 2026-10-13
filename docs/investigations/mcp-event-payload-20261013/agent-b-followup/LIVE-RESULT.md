# Live Work/dot comparison — 2026-10-13

## Current result

A fresh Work subscription received complete native `message.received` payloads for one
singleton and two subsequent messages. A fresh dot subscription produced the exact test
prompt but omitted the entire promised `automations.mcp_event` result. Two later dot test
runs also omitted that result. No product code, runtime or callback format was changed.

This is a live comparison on the same current server/sender implementation, not a
same-event fanout: the two hosts used distinct recipient sessions and distinct subscriptions.
The test does not identify the exact internal platform component or establish a permanent
lack of dot support. The coordinator reported earlier historical dot-success narratives;
the original tool blocks from those earlier successes were not recovered by this reviewer.

## Observed chain

| Control | Sender and server evidence | Receiver evidence | Verdict and limit |
|---|---|---|---|
| Work singleton | One committed native message; persisted event and fresh-subscription delivery captured, one attempt, HTTP 200. | Coordinator forwarded the receiver's actual injected event ID, time, identities and full text, originally recorded before inbox/history. | The event ID and entire text match the saved native send and scoped persisted record. Original Work tool block was not opened directly by this reviewer. |
| Work two-message series | Exactly two committed messages; two persisted events and fresh-subscription receipts, each one attempt, HTTP 200. | Both injected payloads reported before recovery reads. Both blind nonces were forwarded before this reviewer disclosed the expected values; full text was then forwarded. | All reported IDs/times/identities and both entire texts match saved sends and scoped records. Whether Work combined the events into one run was not established. |
| Fresh dot singleton | One committed native message; both original and fresh subscription delivery rows were captured before test disable, each one attempt, HTTP 200. | Coordinator directly recorded the exact test prompt with the entire injected result absent, before inbox/history. | Current missing-payload symptom reproduced for a newly created test subscription. No model-visible event ID exists to independently tie the notification to a stored event; exact test prompt and bounded control window provide the available correlation. |
| Dot two-message series | Exactly two committed native messages. The final read retained two HTTP-200 delivery rows for the original subscription only. | Two distinct singleton test wrappers, both without payload. Neither run provided event IDs. | The fresh test had been disabled before the final read; its delivery rows were no longer available. Do not claim fresh-subscription HTTP 200 for this pair from the original subscription's rows. No per-wrapper event mapping or combined batching is proven. |

All database reads were explicitly scoped to the investigator's exact control message IDs
and expected sender/recipient, used SQLite read-only mode, and followed the relevant blind
receiver observation. The controlled messages included fresh random nonces, UTF-8 text,
quotes and an actual newline. No callback URL, signing secret or authorization token was
exported. Persisted event rows are not historical raw HTTP captures.

The same runtime remained version 0.26.3, commit
`b6b0605d893444ccce50089c6a865891fb63d8f8`, during the Work checks and the dot singleton
correlation. The checked event implementation files remained byte-identical to the sender
used in the local differential tests. The deployment of another development node was handled
by a different investigator; these experiments did not restart the event-source service.

## Test routing and cleanup

The first test agent ran in Codex and lacked an event-subscription/context-delivery path.
It did not execute a delivery test. A real Work conversation with supported event creation
was then obtained. Previously canceled tests were left disabled by this investigator.
Fresh tests were created by the receiving side under the coordinator's explicit instructions.
Each native series was sent only after a separate GO, with exactly the authorized count.
The fresh dot subscription was disabled by its owner after the completed series; the original
subscription was preserved. This investigator performed no subscription mutations.

The coordinator observed different creation metadata: Work used a conversation destination,
while the dot creation route used a thread destination with null conversation ID. This is an
observed difference, not a demonstrated cause. Historical dot-success reports also used thread
routing. No attempt was made to forge a destination, transfer a callback secret, or bypass
normal event authorization.

## Interpretation and next discriminating step

The current native sender can deliver the documented payload into a model context: the
fresh Work controls demonstrate that capability. Together with the actual TLS/body/signature
comparisons against public implementations, this removes the basis for a speculative serializer
or signing rewrite. A failure in the current dot ingestion/routing/context path is consistent
with the observations. Its internal stage and cause are not observable from this server alone.

A stronger remaining experiment is one event delivered to two newly agreed test subscriptions
on a common authorized recipient, followed by both receivers' blind observations. That exact
same-event experiment was proposed but was not performed in this run. Platform-side tracing
of the captured fresh dot singleton should inspect accepted callback persistence, selected
automation destination and creation/insertion of the promised tool-result message. The lack
of that result must not be represented as an empty JSON object or replaced by an inbox read.

Exact operational IDs, nonces, timestamps and native receipts are kept outside the public
repository in a private operator manifest and the conversation's four control-receipt files.
This document is evidence of the current outcome, not a product fix or completed dot acceptance.
