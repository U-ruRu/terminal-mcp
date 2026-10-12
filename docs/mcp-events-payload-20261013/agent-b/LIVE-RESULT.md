# Live control — bounded status, not acceptance

Final bounded outcome: **live control not executed; sender unavailable and native self-send rejected**.

User approval and test automation creation were reported directly by dot/Romeo-5:
`secondary:msg:NdWocP2DqtqPxpz5`, 2026-10-12T21:20:54.087Z.
Platform automation ID: `6acd4f27939c819186c3ea71c2970103`.
Server subscription ID independently read from BacLOUD:
`sub_3135371242a9cae4c8b0937b9229b56de003bc4f`.
It was created at 21:20:41.153Z, expires at 22:20:41.153Z, selects `message.received`,
and pins the test recipient Uniform-156 on BacLOUD. Access number/callback/keys are omitted.
The existing dot subscription was not modified.

A one-message native send was requested from the already active separate sender Alpha-98
at 21:21:14.167Z. The request was read at 21:22:20.638Z (native message history), but this
only establishes that the sender received the test request. It is not a sent control event.
The nonce was not supplied to dot. Self-send was avoided because its recipient separation
is inadequate and the coordinator reported a previous `cannot_message_self` result.
B's inbox remained empty at the later 21:27 read. The current live receipt is appended below
when it is available; an unrelated run must not be attributed to the test.

Dot's latest direct observation received before this checkpoint:

> На21:23:45UTC отдельного запуска с prompt тестовой подписки Uniform-156 ещё не вижу.
> Последний видимый запуск21:21:19UTC относится к основной координационной автоматизации,
> payload отсутствует; приписывать его контрольному сообщению нельзя. BacLOUD inbox не читала.

Source: `secondary:msg:OadFHoVpVAWzRexr`, created 2026-10-12T21:23:55.442Z.
This observation is deliberately **not** a negative result for the new control event.

## Required continuation of this test

Confirm one native control delivery and its recipient-scoped event/outbox row. Preserve
its eventId and text away from dot until she records the actual visible result. Only after
that observation compare the values. If single delivery is correlated, obtain dot's go-ahead
for two new separate messages to inspect platform batching. Stop/discontinue only the agreed
temporary test automation afterwards; preserve the original coordinator subscriptions.

No batch live test is claimed at this checkpoint. No exact live HTTP body/header/response
capture is claimed; the outbox retains status, while transport-capture.json is explicitly
an injected sender-boundary fixture. Outstanding capture and platform-side correlation
are called out rather than replaced with reconstructed signatures or response bodies.

## Final outcome and cancellation

At 21:29:33.250166 UTC, read-only BacLOUD command `f3c38b0a` found **zero**
deliveries for the test subscription. The separate rollout had moved the runtime to
`b6b0605d893444ccce50089c6a865891fb63d8f8-07820ca3ecd1`; all three inspected event-source
SHA256 values remained identical to the tested baseline.

Papa-5 repeated connector discovery and reported that only code-free BacLOUD read tools
were available to him; Access/session/message writes were absent. He could not act as a
second sender without violating the access constraint. Source: `secondary:msg:crqdMSMcGLr99h_U`.
The pending Alpha-98 control request was explicitly cancelled with native message
`bacloud:localmsg:5670b3f249b1b32283fa4e7dce3cc92e`; cancellation delivery was acknowledged
by the messaging service, not claimed read by Alpha. No second Alpha request was made.

B then attempted one normal `message/send` to the approved test recipient Uniform-156,
using B's own identity. The actual connector response was:

```json
{"ok":false,"error":{"code":"cannot_message_self","message":"Нельзя отправить сообщение самому себе","outcome":"not_committed","retry":"reconcile"}}
```

No authorization bypass or alternate internal send was attempted. This is a concrete test
setup blocker, not a reproduced missing-payload result for the temporary automation.
The batch phase was not started. Dot was asked to disable only the agreed temporary
automation in `secondary:msg:TGEcbV099i8EjZu0`; her confirmation is recorded separately
when available. Original subscriptions remain outside B's mutation scope.

Minimum next step: an available separately authenticated native sender sends one message
to the approved attached receiver; dot records the supplied tool result before any inbox
read. Revalidate the receiver and create a fresh approved test subscription if the old
window has ended. Do not reuse a stale session number from this report.
