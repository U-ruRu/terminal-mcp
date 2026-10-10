# LIVE-021-20261010-A — outcomes, retries and latency

## Scope and verdict

Owner Alpha-3 on Secondary. Legacy session 21:32:34–21:55:34 UTC on 10 October 2026. Base `bde0dec8bd0679c3cbda768104672de57956b6cf`, verified against local and remote `integration/M3-functional-candidate`. Worktree `/workspace/terminal-mcp-live021-alpha3-20261010`; branch `task/TMCP-LIVE021-OUTCOMES-alpha3-20261010`.

Bravo-3 initially collected command/proxy evidence, then DOT reassigned him to local-message status work. His evidence-only handoff is published at `e5f3ab951c9d26d5278a6095a1455ceac33dc985` on `task/TMCP-LIVE021-INGRESS-bravo3-20261010`. Alpha owns this combined investigation. DOT changed from X-ray-2 to Foxtrot-3 at 21:40:46 UTC.

**A reproducible server defect was fixed:** exceptions in optional telemetry could replace a committed task-update receipt or the original cancellation, and prevent context reset. The live MCP role endpoints did not use that telemetry wrapper at this SHA; this defect is **not established as the cause of the live incidents**.

**Live root-cause closure is partial:** two distinct command records correlate with two proxy attempts; the content update and first BAC-B comment were durably committed. The actor initiating subsequent HTTP attempts and the hop losing or rejecting replies remain unproven. A minimal diagnostic chain is now implemented for the next isolated acceptance/release. No speculative command deduplication, timeout-policy change, canonical integration or deployment was performed.

## 1. Two command executions

| Command | Enqueued UTC | Claimed UTC | Finished UTC | Queue wait | Execution | Exit |
|---|---|---|---|---:|---:|---:|
| `70d86f06` | 20:56:04.087 | 20:56:04.126 | 20:56:04.376 | 39 ms | 250 ms | 7 |
| `cac560e1` | 20:56:47.474 | 20:56:47.496 | 20:56:47.641 | 22 ms | 145 ms | 7 |

Both records are in queue 1, sequence 2 and 3, status `failed`, error null. Command text equality was checked on FirstByte without exporting command contents. These are two durable operations, not two observations of one command hash.

Caddy contains distinct Executor POSTs for the same session-header equality group S5 and different traceparent groups T38/T43:

- 20:56:03.252–20:56:04.512, 1.260589960 s, HTTP 200, 1474 response bytes.
- 20:56:46.617–20:56:47.764, 1.146998550 s, HTTP 200, 1477 response bytes.

Each claim lies inside its unique active Executor POST. Another Executor POST in S6 ends at 20:56:47.420, before the second allocation. Proxy start times are completion timestamp minus recorded duration. Receipt event 56 on the shared task independently records the tester's one caller-level invocation and both hashes.

**Conclusion:** two observed HTTP attempts correlate with two durable command registrations. The proxy did not record JSON-RPC bodies/tool names or a direct attempt-to-command link. Payload equality at HTTP ingress and the retry initiator are not proved. HTTP 200 is not client receipt acknowledgement. Deduplicating by command text, time window or JSON-RPC ID would merge legitimate independent commands and was not introduced.

## 2. Content update committed while the caller saw UNAVAILABLE

Namespace `LIVE-021-20261010-A`; task `TASK-70ABD8FCEC34EAD1E17C2DEB`.

- Work event **46**, `updated`, 21:01:55.792 UTC: fields `[description]`, `cas_expected_revision=1`, author group A3.
- Work event **47**, `persistent_mutation`, 21:01:55.812 UTC: action `update`, same author. The backend reached its successful-result audit path.
- Shared receipt event **56** records the caller's `UNAVAILABLE: McpServerError: Connection failed`, then readback at approximately 21:02:46 confirming exact CONTENT-A description and revision 2. Receipt event **60** independently records another caller's revision-conflict response with current revision 2.
- The read-only capture at 21:36:50 still found CONTENT-A. Current revision was **3**, explained by the later archive event **65** at 21:06:47.061 and audit **66**. Revision 3 must not be misreported as the incident revision.
- Candidate correlated Coordinator POST S4/T113: 21:01:55.440–21:01:55.884, 0.443185593 s, HTTP 200, 197 bytes.

**Conclusion:** the write committed. The later audit also exists. No evidence ties this incident to the telemetry defect fixed here; the recorded proxy result is consistent with a short server attempt, but cannot establish successful JSON-RPC delivery. The exact lost/failed hop remains unknown. Recovery is a normal `task_get`/history read of this known task and comparison of marker, revision and events; a blind write retry is unsafe.

## 3. Client waits up to approximately 83 seconds

BAC-B reported `task_get` comments history from 20:59:48.272 to 21:01:11.223 UTC: **82.951 seconds**, ending in `The tool failed internally.` This interval is preserved in shared receipt event 48.

The completed-proxy window 20:54–21:05 contains **146** HTTP records, with maximum completed POST duration **5.610609498 seconds**. Candidate S1 Coordinator attempts for the history interval are:

| Proxy interval UTC | Duration | HTTP | Response bytes |
|---|---:|---:|---:|
| 20:59:48.411–20:59:48.901 | 0.489261663 s | 200 | 1324 |
| 21:00:28.962–21:00:29.361 | 0.398498898 s | 200 | 1646 |

The approximately 40-second inter-attempt gap is not command execution time. Observed command queue waits and runtimes are separately measured above. Server queue/storage wait durations for these specific task requests are unavailable; no SQLite-lock cause is claimed. Completed proxy records alone cannot exclude an unfinished request, a wait before ingress, intermediary retries or caller processing after send.

The application event files have **zero records in this incident window**; their last record is `application_started` at 20:15:30.971. Source inspection explains the observability gap: MCP role functions did not call `telemetry.observed`. The systemd journal records `CallToolRequest` without the tool/receipt correlation needed here. FirstByte service start 20:15:27 precedes all incidents; there is no incident-time restart indicated by this current process start.

**Conclusion:** the available completed HTTP durations do not explain the caller's 83 seconds. Blaming server queueing, storage or the client specifically would exceed the evidence.

## 4. BAC-B duplicate_comment

The shared task is `TASK-7D3B5EC33410DCE4ABED08E5`.

- Event **48**, comment, 21:02:01.199 UTC, author group A2, contains BAC-B's report receipt.
- Event **49**, `persistent_mutation`, 21:02:01.235 UTC, same author, confirms the successful comment path.
- Candidate S1/T114 POST: 21:02:01.062–21:02:01.332, HTTP 200, 169 bytes.
- Candidate later S1/T116 POST: 21:02:41.752–21:02:42.537, HTTP 200, 491 bytes.
- Caller interval supplied in the task: 21:01:59.905–21:02:42.616, final `duplicate_comment`, `outcome=not_committed`.

`TaskStore.append_comment` at this base checks exact text **per author across the task's entire comment history** in the same transaction as the append. `TaskCoordinator._action_comment` maps that rejection to `duplicate_comment`. `docs/connector-runtime-contract.md` and existing tests explicitly specify this policy.

**Conclusion:** `not_committed` describes the rejected duplicate attempt, not absence of the earlier committed comment. The existing receipt and two temporal proxy attempts support a first-commit/later-rejection chain. The second RPC payload and caller-level intention are unavailable; an intentional independent second comment is not established. The current exact-text-per-author policy is retained, not silently redefined as operation-id idempotency. It cannot distinguish an intentional identical comment from a retry. Supporting both would require an explicit operation-identity contract; that remains a separate policy question, not a basis for arbitrary command deduplication.

## Confirmed defect, fix and diagnostics

`telemetry.observed` performed metrics and event writes in `finally` without isolation. An exception there replaced a successful operation result and skipped ContextVar resets. Cancellation was also replaced by a diagnostic exception. The new SQLite test commits CONTENT-A at revision 2, injects failures in event/metric sinks, and checks that the same committed receipt survives. A separate test checks original cancellation and context cleanup.

Before the fix: **3 failed**, pytest exit **1** (`telemetry-red.log`). After the fix plus existing public-error boundaries: **6 passed**, exit **0** (`telemetry-green.log`). Optional metric/log calls are now isolated; operation errors keep existing public-error normalization, cancellation propagates, context resets in a nested `finally`.

New payload-free diagnostics emit:

1. `mcp_http_started` and `mcp_http_finished`: server trace ID, known role, request-body completion timing/byte count, response status, successful ASGI send flags/byte count, duration, observed disconnect and exception class.
2. `mcp_tool_started` and `mcp_tool_finished`: independent tool-attempt UUID, role/tool, duration, returned success/error flag and whitelisted command/task receipt identifiers when present.

Raw commands, arguments, response bodies, credentials, callback URLs and raw caller correlation headers are excluded. Upstream request IDs are represented only by a fingerprint in these events. All four event names are retained at minimal logging level using the existing bounded logger queue and rotating files. Diagnostic failures preserve the result, original exception and cancellation. No observer activity touch is added. The existing HTTP timeout is unchanged; an application-raised TimeoutError is distinguished from expiry of that HTTP deadline.

A successful ASGI send means the next ASGI layer accepted bytes. It is **not** proof that ChatGPT received or accepted a successful RPC response. `mcp_tool_finished` reports a returned receipt, not exactly-once semantics or proof that every successful no-op mutated state. Calls interrupted before any receipt is returned may still require normal read/recovery.

Shared edits were approved by Foxtrot-3. Zulu-2 approved the single app.py middleware argument change; his Events composition hunk is elsewhere. roles.py only gains the diagnostic installer after the pre-existing activity wrapper. Generator descriptions, task_get pagination, OAuth, message statuses and Events logic are untouched.

## Verification and safe next step

The isolated ASGI harness uses temporary SQLite databases, the real PersistentBackend/TaskCoordinator, harmless `printf safe` command text and a mocked terminal submitter. It injects failure while sending the response body, verifies durable readback, and submits a deliberate independent call with the same JSON-RPC ID, preserving two hashes and two submissions. It also verifies task readback after loss, concurrent trace isolation, exception/cancellation preservation, unchanged role schemas and minimal-logging retention.

Initial focused regression: **109 passed**, 1 existing dependency deprecation warning, exit 0. Additional final diagnostic tests: **16 passed**, exit 0. Final repeated focused suite: **116 passed**, 1 existing dependency warning, **35.79 seconds**, pytest exit **0**; Ruff and diff checks exit **0**. Exact commands and source/log hashes are recorded in `VALIDATION.json`; full repository suite and live deployment were not performed by this agent.

Artifact directory: `/workspace/terminal-mcp-artifacts/live021-alpha3-20261010`. The publication checkpoint includes final source SHA, remote verification and command receipts. `evidence.json` contains the selected privacy-reduced evidence; full raw task-comment text remains only in the bounded investigation artifact on Secondary and is not committed.

Next safe step: independent review of the pinned candidate, integrate the approved disjoint hunks, then an explicitly authorized isolated/live acceptance using these attempt traces. For the next lost response, preserve caller invocation/attempt metadata, proxy trace fingerprint, server trace ID, tool attempt ID and command/task receipt. Read existing state before retrying an uncertain write. Do not deploy or add an idempotency key based solely on these incomplete historical traces.
