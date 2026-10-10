# TMCP Events/message.received — independent QA, Golf-3

UTC: 2026-10-10, 21:48–22:00. QA: Golf-3 / Events-QA, Secondary legacy session, expires 22:11:39Z.
Coordinator: Foxtrot-3 dot; accepted preliminary BLOCKING at 21:58:21Z; a different developer owns any correction.

- Exact reviewed implementation: 2398bc3d254b55bc726e5d6c99836601cd3a95f0
- Source branch: task/TMCP-MCP-EVENTS-MESSAGE-zulu2-20261010
- Source parent: 3cc90107eca8b34ffa28bd40dfa212f1d28d77f1
- Documentation source: aa011e806ad4e70cd7725f49a03c7998701a60a3
- Independent QA branch: qa/TMCP-MCP-EVENTS-MESSAGE-golf3-20261010
- Independent worktree: /workspace/terminal-mcp-events-qa-golf3-20261010
- Raw test logs: /workspace/terminal-mcp-artifacts/golf3-mcp-events-message-qa-20261010/

## Final verdict: BLOCKING for local candidate

The Access slot can be suspended during the asynchronous callback-verification challenge after initial identity resolution. The current events/subscribe still returns a subscription ID and persists a private-message subscription binding for the revoked slot.

Source sequence: src/terminal_mcp/mcp/event_server.py resolves the recipient before awaiting verify_callback, then invokes MCPEventStore.save_subscription without checking the slot and credential a second time.

Independent reproduction: tests/test_mcp_events_qa_golf3_revocation.py::test_revocation_during_challenge_must_not_save_private_subscription. Its isolated sender suspends the real disposable Access Mesh slot while responding to the webhook verification challenge. Server returns success instead of error; FINAL_QA.log captures failure of the required security assertion.

Impact qualifier: independent tests confirm delivery rechecks the slot and suppresses an already-pending callback after revocation. No private-message leak was demonstrated. The defect is subscription persistence and success after revocation. Do not claim unauthorized message delivery.

Required separate fix: after webhook verification, revalidate current OAuth/bearer credential and exact Access binding (hashed connection key, issuer, slot, immutable logical agent, active state, scope) immediately before persistence. Bind the comparison and SQLite write to one transaction where possible; fail closed without rows if any binding has changed. Add a deterministic verification-time revocation race regression. Preserve original events and contracts; no recipientGrant.

## Evidence and methodology

- INTEGRATION.log / INTEGRATION.exit=0: 132 PASS, 0 FAIL, one Starlette deprecation warning, 86.78 seconds. Independent invocation of 13 modules covering Events, managed active/idle/end, Access Mesh metadata, grants, messages, sessions and OAuth.
- BASELINE.log / BASELINE.exit=0: 40 PASS, 0 FAIL, one warning, 16.65 seconds. The three Events modules are included in the above 132, not an additional 40 unique cases.
- FINAL_QA.log / FINAL_QA.exit=1: 4 PASS + 1 intentionally failing required-security assertion, one warning. The failing case exposes the confirmed blocker. INDEPENDENT_PASS.log excluding only that known failure: 4 PASS, exit 0.
- Ruff check, Ruff format check and diff whitespace checks: PASS.
- TestClient and synthetic OAuth/JWT, local SQLite, mock HTTPS callback. No production client or server touched.
- A discarded exploratory hypothesis about AuthFoundation grants is not applicable to the Access issuer path (it does not create those grants in this fixture). Exploratory REVOCATION.log is not a finding or release criterion.

## Per-contract judgment

| Contract criterion | Isolated QA | Evidence / qualification |
| --- | --- | --- |
| Actual local attach; no false attach on remote grant/Access issuance; correct name/origin | PASS | QA native ASGI case starts Access A: zero attached events; different-pair Coordinator attach creates one attached event with local server and agent name. Broader Events pack covers remote-only attachment exclusion. |
| Inactive strictly after 180 seconds, one shot per episode, rearm after real activity, observation inert, explicit end and timeout | PASS | Events and managed-live regression suites repeated in 132-pack. Synthetic local clock, not physical runtime monitoring. |
| Distinct metadata across Access/Coordinator, same immutable agent; duplicate display names isolated | PASS | QA native ASGI test A Access vs A Coordinator distinct pairs and B separate identity. Developer same-name isolation test independently rerun in 132-pack. Metadata alone / different OAuth client does not subscribe. |
| Active rights/revocation and private delivery binding | BLOCKING | Suspended Access slot denies fresh subscribe before challenge and suppresses pending callback (two QA tests PASS), but challenge-time revoke still saves a subscription (one QA test FAIL). |
| Committed local message, payload, idempotent retry, no ACK/read/reply side effects | PASS | Native Coordinator message yields signed callback with actual server, sender/recipient, message_id, original Unicode text, UTC timestamp. Independent raw-body HMAC check, forced retry stable eventId, exactly one message, unchanged read_at/replied_at/seen_count. |
| Callback challenge/signing/error handling and true dot receipt | PASS locally; real dot NOT TESTED | QA mock verification and signature PASS. Existing independent rerun covers SSRF URL restrictions, 410/413 no-retry, bounded retry, rotations and error redaction. No live callback registration into ChatGPT. |

No client credentials, callback secrets or message content were observed in the error/log surfaces exercised by local tests and code review. This does not constitute a production logging audit.

## OPEN architecture and live criteria

1. Access-per-working-node: local Coordinator/Executor B can attach a foreign session number and link a distinct subject+conversation to the same immutable logical agent. Access B lacks an authorized foreign Access attach-by-number path; its ordinary start is an issuer operation and would create another identity. Need an explicit trusted Access-role binding for B to the existing issuer/slot and a resolver check of that recorded binding. Require current OAuth principal, scope, immutable recipient and revocation, never a display name. Prefer evaluating this smaller topology first.
2. Single aggregator Access A: B-local message and event journals do not reach A. This would require an authenticated and deduplicated event-only B→A relay using existing mesh peer trust, preserving origin server; ordinary private messages remain local. This relay was explicitly deferred and was not written or tested.
3. Whether actual ChatGPT events/subscribe carries standard params._meta (as tool calls do) is unverified. During dot-approved rollout inspect safe UTC ingress diagnostics and binding outcome; missing metadata must fail closed. Do not identify recipient by public name, OAuth client alone, or empty arguments.
4. Real user acceptance requires approved fixed SHA deployment, genuine dot OAuth events/subscribe and callback challenge, local native message to exact intended agent, signed webhook receipt, origin server, correct payload, duplicate/ACK behavior, permission revocation. ALL NOT TESTED at the real platform and fleet.
5. Cross-server delivery, Supervisor, FirstByte/BacLOUD rollout, Canonical and protected OAuth state NOT TESTED and untouched.

## Boundaries

Only a standalone QA worktree and tests/docs were changed. No implementation file under src changed, no runtime restarted, no deployment, no cross-server relay, no Bravo-3 status changes. Coordinator explicitly directed to hand off the blocker to a separate developer; independent QA must not mix fixing with acceptance.
