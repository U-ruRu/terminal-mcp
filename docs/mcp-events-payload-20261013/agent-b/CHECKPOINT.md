# Checkpoint — acceptance remains open

B branch: task/mcp-events-payload-quebec5-20261013.
B worktree: /workspace/terminal-mcp-events-contract-uniform156.
Task: terminal-mcp/TASK-B800E5. Source-review A commit: bbb7f5cf71b6477ff3fa7fe2115f9fd8191f8367.
Read REPORT.md, LIVE-RESULT.md and PEER-REVIEW.md before continuing.

Confirmed: four focused tests PASS; three synthetic sender-boundary captures verified;
production event source hashes match inspected4ecb720 and remained unchanged after
separate b6b0605 rollout. Historical outbox32 sent/200; stored message data populated.
Exact historical HTTP bodies/signing timestamps/responses are not retained by the outbox.

Live blocker: second sender unavailable; direct self-send rejected cannot_message_self,
not_committed. No control event created and no batch test. Temporary automation shutdown is confirmed by B native update at21:32:37.677003UTC,
is_enabled=false, last_run_time=null. Revalidate a new approved test before continuing.
Original dot subscriptions, production runtime and identity-conflict fix are untouched by B.

Next: acquire a separately authenticated native sender and attached test recipient;
coordinate one temporary message.received subscription with dot; keep control IDs/nonce
away from her until she records the webhook result. Correlate eventId and exact text,
then approve two distinct messages for batching. Save exact allowed HTTP diagnostic
receipts if the platform/runtime provides them; do not invent missing fields.

No payload fix has been established or deployed. If matching requests still reach200 and
automation launch but no injected result, hand the safe IDs/times to platform engineers
for callback storage/context-insertion tracing. Public Recly MRE is referenced by A.
