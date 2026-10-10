# LIVE-021 ingress evidence checkpoint — Bravo-3

2026-10-10 21:36 UTC. Base: bde0dec8bd0679c3cbda768104672de57956b6cf, local and origin/integration/M3-functional-candidate verified equal. No production mutation, test command, deployment or runtime restart. FirstByte collection used existing SSH alias, SQLite URI mode=ro and bounded journal/Caddy reads; artifacts only written on Secondary. No source changes. Coordinator X-ray-2 reassigned Bravo-3 to local-message status correctness; Alpha-3 owns ALL remaining LIVE-021 work.

## Incident 1 — observed chain, not attribution of retry initiator

| command | enqueued UTC | claimed UTC | finished UTC | queue wait | execution | exit |
|---|---|---|---|---|---|---|
|70d86f06|20:56:04.087|20:56:04.126|20:56:04.376|39 ms|250 ms|7|
|cac560e1|20:56:47.474|20:56:47.496|20:56:47.641|22 ms|145 ms|7|

Both durable rows are status=failed, error=null, queue 1; sequence 2 and 3. Stored command text equality is confirmed on-node; text is not exported. Caddy contains separate Executor POST attempts for the same x-openai-session equality group S5 and distinct traceparent groups T38/T43:

- 20:56:03.252–20:56:04.512, duration 1.260589960 s, HTTP 200, response bytes 1474.
- 20:56:46.617–20:56:47.764, duration 1.146998550 s, HTTP 200, response bytes 1477.

Each claim falls within its own unique active Executor POST. Another Executor POST (S6) ends at 20:56:47.420, BEFORE the second queue allocation/claim, so does not overlap that claim. Start times are Caddy completion log timestamp minus duration. This establishes separate observed HTTP attempts correlated to separate durable commands. Missing: RPC body/tool name and attempt-to-command identifier, caller invocation/attempt sequence and last acknowledged response at intermediary hops. Retry initiator is NOT proven. HTTP 200 and bytes written do NOT establish that ChatGPT consumed a successful RPC result. No text/window/JSON-RPC-ID deduplication is justified.

## Incidents 2/4 — evidence for Alpha-3

Candidate correlation for update committed at 21:01:55.792: Coordinator POST S4/T113, 21:01:55.440–55.884, duration 0.443185593 s, HTTP 200, response size197. At this checkpoint this is time/route correlation, not proven tool/RPC identity. Data/receipt lookup remains with Alpha.

Candidate BAC-B duplicate-comment sequence: Coordinator S1/T114 21:02:01.062–01.332, size169; S1/T116 21:02:41.752–42.537, size491; both HTTP200. Whether an initial commit caused a later replay rejection requires canonical task receipts; do not infer from response size alone.

## Incident 3 — bounded evidence / remaining gap

146 HTTP records (all methods) exported safely for completion window 20:54–21:05 UTC. Maximum logged POST duration is 5.610609498 s. Candidate S1 history sequence: 20:59:48.411–48.901 (0.489261663 s,size1324) then21:00:28.962–29.361 (0.398498898 s,size1646), HTTP200. User client window20:59:48.272–21:01:11.223 is82.951s. There is no observed single 83-second completed POST in this window. Precise endpoint/tool association, uncompleted ingress, upstream/client waits and per-storage timing remain unmeasured; do not claim the entire problem is external or blame SQLite without evidence. Do not treat gaps between attempts as server execution duration.

## Artifacts and next step

- collect_live_readonly.py: exact collection program, no credentials/command text/raw identity header values exported. Header equality labels are scoped to the file only.
- firstbyte-safe-evidence.txt: UTC timing/routes/status/sizes/equality groups and two durable command rows.
- Secondary /workspace/terminal-mcp-artifacts/live021-bravo3-20261010/firstbyte-journal-selected.txt: request-type journal lines and DB path discovery. Journal contains CallToolRequest only; no request-to-command correlation.
- FirstByte runtime symlink observed /opt/terminal-mcp/releases/20261010T200243Z; terminal-mcp.service PID3757686 started20:15:27 UTC. No restart around incident was observed from this current process start.
- Historical source /workspace/terminal-mcp-handoffs/TMCP-TRIPLE-EXEC-STAB02-SIERRA.md is background only. This new incident was independently read.
- Existing tests/test_command_committed_receipts.py and tests/test_command_replay.py found; not run by Bravo at reassignment. Code tests and full regression: NOT RUN; evidence-only checkpoint.
- Collection commands ee49d664, a2c1a563, b22aa835,2418f408 exited0. d7a99ef1 exited0 but rg was absent; subsequent inspection used grep.

Alpha-3: obtain receipt/task location from configured service (tasks not found in selected /var/lib/terminal-mcp SQLite schemas), correlate producer/server receipts, add minimal safe correlation at ingress/accepted command/response failure in an isolated instance. Use request IDs only for correlation. Implement a server fix only after reproducing the actual failure, preserve intentional repeated operations. BacLOUD evidence and full client attempt sequence remain uncollected at this handoff. No deploy.
