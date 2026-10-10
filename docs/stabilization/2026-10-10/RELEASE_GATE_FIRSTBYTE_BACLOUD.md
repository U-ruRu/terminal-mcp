# Terminal MCP — integrated release gate / FirstByte → BacLOUD

**Prepared 2026-10-10 UTC by Uniform-2, Secondary; coordination Quebec-2 (dot).** Preparation only. No production/network/database write permissions delegated. Current accepted Canonical at assignment: `integration/M3-functional-candidate` `8ea0a40de0e059ae16640adf2f0163f03d20a850`. The release SHA remains **UNFROZEN** until the integrator and independent QA approve the full merge with Local Messaging and MCP Events.

## 1. Governing scope and evidence rules

Inputs reviewed in full: `/workspace/terminal-mcp-stabilization-plan-20261010/docs/stabilization/2026-10-10/{PLAN,TASKS,EVIDENCE_MATRIX}.md`, release 0.20.9/operator evidence, STAB-05 alignment and latest Oscar-2 localmsg+SQLite handoff. STAB-06–11 are *acceptance lanes*, not evidence of passes. Keep four separate states for every row: **source integrated**, **tested on exact SHA**, **installed**, **live accepted**. Older 0ac13c8 full suite (2060 PASS) and prior live observations do not validate the upcoming combined SHA.

**Current messaging contract (overrides obsolete mesh-message assumptions):** `recipients`, send, inbox/history, ACK and reply operate **only within each physical server**; messages expire after 24 hours from creation, with independent reply TTL. Explicit legacy fleet message routing is disabled and produces no new remote message jobs. A replicated identity or Mesh control event is still an inter-server coordination mechanism and must retain its own contract and provenance. No inter-server message delivery acceptance is required.

Evidence discipline: exact 40-digit Git commit, clean checkout before/after, Python/SQLite ABI, targeted test log plus exit, independently owned full-suite log, approved isolated/real OAuth contexts, source/installed fingerprints, PID, release metadata, service health, sanitized before/after counts, UTC times, test namespace. Every claim of PASS has an artifact path and an owner. Do not infer a test PASS from `task done` or from successful HTTP health alone.

## 2. Source critical gate (not full suite)

Runner is **separate QA worktree** `scripts/stab_release_gate.sh`. It has no install/update/restart/credential/topology/database deployment operations. It uses existing committed test modules and `tmp_path`, enforces an exact clean source SHA, writes output only under short `/tmp` path, records test-set checksum, runtime SQLite ABI and final SHA; refuses missing final-only tests. Timeout 420s. It does not duplicate Oscar-2's full suite. The *exact target checkout* is passed via `--repo`, so do not rebase a frozen source while it runs.

Preparation (current Canonical; source-only, missing post-merge tests intentionally excluded):

```sh
/workspace/terminal-mcp-release-gate-uniform2-20261010/scripts/stab_release_gate.sh \
  --repo /workspace/terminal-mcp-integration \
  --expected-sha 8ea0a40de0e059ae16640adf2f0163f03d20a850 \
  --python /workspace/terminal-mcp/.venv-test/bin/python --mode prepare
```

**Final only after Quebec-2 freezes `<FULL_MERGED_SHA>` and QA confirms acceptance.** Use a short-lived clean worktree checked out at precisely that SHA; use the release-approved Python + SQLite ABI (plan requests SQLite 3.53.4; source helper 3.46.1 alone is insufficient). The following is a template, not permission to release:

```sh
scripts/stab_release_gate.sh --repo /workspace/<frozen-clean-source> \
  --expected-sha <FULL_MERGED_SHA> --mode final \
  --python /path/to/pinned/venv/bin/python --expected-sqlite 3.53.4
```

Final mode requires existing `tests/test_local_messages_24h_victor.py`, `tests/test_mcp_events.py` and `tests/test_console_events.py` on the *same commit*; absence is **BLOCKED**. The runner covers SQLite integrity/worker deadline/cleanup/symlink/lock and backup fixtures; session restart/expiry; command replay, distinct identical runs, fast read, durable cancel and observed receipts; Task API six states/CAS/receipts/comments; standalone local messaging; Mesh identity/outbox, physical TCP partition source fixtures. Final adds 24-hour message boundaries and Events protocol tests. A `source_gate=PASS` is explicitly **source-only**, never a complete release sign-off.

## 3. Release acceptance matrix

| Lane / gate | Verification | Expected result | Required environment / state as of preparation |
|---|---|---|---|
| STAB-11 manifest | dot freezes sole integrated source SHA; `git status --porcelain`, ancestry/diff, generated MCP schemas, complete source fingerprint, release metadata | exact same SHA in test log, approved manifest and candidate, clean tree; no schema drift | Secondary clean frozen worktree; **PENDING final SHA** |
| STAB-11 quality | Oscar-2 full pytest once, independent QA, `ruff check .`, `scripts/check_repository_privacy.py`; critical runner separately | exits 0 on one exact candidate SHA; no unresolved P0 or discarded failures | isolated CI/test venv; Oscar-2 owns full suite; **PENDING** |
| SQLite health | `test_sqlite_main_health.py`, `test_sqlite_posix_lock_preservation.py`; corrupted B-tree/index, symlink retarget, SQL timeout, cancellation/reopen, public health | fail closed (`storage:error`, `status:failed`) for corruption/deadline; no leaked native worker; healthy recovery after safe test repair; POSIX locks preserved | temporary SQLite files; pinned SQLite ABI for final; patch in 8ea; **prep testing** |
| STAB-10 persistence/backup | existing `test_backup_manifest.py`, real-process and restart tests; bounded extra soak **10 min / ≥500 completed ops / 3 managed test restart cycles**; online backup to distinct copy; restore only in isolated sandbox; `PRAGMA integrity_check`, `foreign_key_check` | counts/side effects/receipts/history exact; integrity_check `ok`, foreign keys no violations; no disappeared committed data | isolated disposable SQLite/process; no working DB restore; **stress/restore NOT RUN** |
| STAB-06 sessions | existing lifecycle, session restart, local attach and recovery tests; fresh start→attach→read→ACK/reply→end, warning/expiry→reconnect | no duplicate active identity; expiry/warning cannot extend lifetime; history and separate agent scope preserved through reboot | disposable local process and test identities; **real provider-bound OAuth separate** |
| Commands / STAB-02/03 | run→read→cancel→receipts; concurrency/recovery; two independent same-text commands and one simulated lost-reply replay | one durable hash per accepted invocation, exactly corresponding side effects; **two intentional identical invocations remain distinct**; no false success after cancel | sandbox command executor, isolated marker side effects; current transport-retry incident **open until attributed** |
| STAB-05 Task API | `task_mcp_contract`, `task_create_request_receipt`, `task_revision_side_effects`, `task_qa_public_state`, append-only checkpoint tests | six states incl. `qa`, state idempotence; content CAS independent; create replay one task, separate intent separate task; append-only attributable comments/checkpoints | ephemeral task namespace/DB; current six-state contract, not old five-state spec |
| Local messages 24h | final `test_local_messages_24h_victor.py`; notify/ack/alert; recipients, send/inbox/history, ACK/reply, expiry boundary, restart | same-server only, TTL 24h, ACK once, independent reply expiry, no peer queue/send, old peer requests terminally rejected; no message/history resurrection after expiry | merged candidate schema/fixtures, isolated pair for negative cross-node assertions; **await integration** |
| MCP Events source | final `test_mcp_events.py`+`test_console_events.py`; authenticated discover/list/subscribe/unsubscribe, signed retries, source boundary | local `agent.attached` once post-commit; `agent.inactive` strictly >180s one-shot/rearm on target activity; `session.ended` manual/hard-expiry once; stable eventId; no observer-induced activity | merged Events SHA; isolated tests; **await independent QA** |
| **REAL OAuth/MCP** | approved genuine provider-bound OAuth, authorized scope/audience, reconnect from new caller, issuer isolation, real JSON-RPC initialize/tools, denied cross-agent attach | correct authorized identity, grants/errors, expiration and history, no hijack or trust crossing | explicitly approved OAuth test client/context + staging endpoint; **NOT REPLACED by mocks; PENDING** |
| **REAL Events → dot** | dot creates authenticated MCP Events subscription, HTTPS challenge, signed webhook on actually attached session, real idle >180s, rearm, end/expiry; dot receipt timestamps + eventIds | dot receives one correctly attributed signed event per business transition, no extras; retry stable ID; unsubscribe stops delivery; no sensitive payload | real dot receiver, approved callback/OAuth, staging service & clock; **PENDING**, not source-test PASS |
| STAB-07 Mesh | same exact SHA on two isolated QA nodes; authoritative identity/event times, delayed/lost end/partition, expiry and anti-entropy, immutable author history | convergence without ghost active identity, unrelated identities distinct, source origin retained | exclusive same-version physical pair, temporary stores; **physical acceptance pending** |
| STAB-08 Mesh lifecycle | forward detach→standalone→fresh enrollment→rejoin, desired/applied, out-of-order ACK, test mesh rename/remove | no stale peer membership, no unauthorized adopted peers or resurrected sessions | same isolated pair, after 07 releases lane; **pending** |
| STAB-09 VPN/event | quiet idle interval measured DB writes/outbox churn; isolated test-link loss/recovery, outbox draining, peer trust, excluded peer no access | bounded retry/convergence, no needless churn; healthy tunnel and application state both correct | same isolated pair after 08; no production network changes; **pending** |
| FirstByte canary | pre/post read-only node probe, approved backups, PID↔current release↔metadata↔source manifest↔`/health/live`; isolated local command/message/Task/history smoke | approved full SHA evidenced by installed-file manifest against frozen Git archive (metadata SHA is shortened), correct version/fingerprint and running process, healthy storage, correct receipts/ACK/reply, no lost history | first target *only after explicit deploy approval*; **not run** |
| BacLOUD canary | same procedure after FirstByte signoff; then inter-server coordination on same SHA | both instances matching exact source and schema fingerprints, independently healthy, converged Mesh control, no peer-message delivery | second approved target, compatible mixed-version transition; **not run** |
| Rollback compatibility | preapprove last compatible **dispatch-disabled** binary, verify schema24 reader, backup/checkpoint and reversible release symlink switch | previous working code can run against unchanged authoritative DB; no automatic DB/schema/data rollback or old remote-message resend | release owner + backups + rollback build verified **before** canary; **pending** |

## 4. Explicit OAuth and Events acceptance (cannot become a source-test checkbox)

Run only in explicitly approved staging/test identities. Observe three distinct channels: local service journal/SQLite event or validated feed, authenticated HTTPS webhook delivery receipt, and **dot receiver record**. Capture UTC times, node, business event name, eventId, attempts/status, counts and sanitized payload (agent_name, server, optionally existing short task only). Never put OAuth tokens, webhook secrets or raw user content in evidence. A remote grant/start alone does not emit `agent.attached`; a *local committed attach* does. At t=180s inactive event must not fire; strictly later exactly once; observer read/another agent heartbeat must not rearm; target action rearms. Manual end or hard expiry produces `session.ended` once per affected local presence. The old 23-minute limited session remains unchanged. Check actual discover → subscribe → challenge → deliver → unsubscribe against dot over signed HTTPS and test reconnect/grant separation with actual provider-bound OAuth.

## 5. FirstByte/BacLOUD read-only preflight and staged execution

Before touching any host, release owner records a frozen manifest: approved full SHA, exact schema/version fingerprints, package version+release ID, exact test logs/exit, SQLite ABI, permissions, confirmed compatible rollback artifact and online backup proof. Run the following read-only block **independently** on FirstByte and BacLOUD; treat `/opt/terminal-mcp-src` as a historical checkout, not the active source:

```sh
date -u +%FT%TZ
systemctl is-active terminal-mcp.service
systemctl show terminal-mcp.service -p MainPID -p ActiveState -p SubState -p ExecMainStartTimestamp -p ExecStart
readlink -f /opt/terminal-mcp/current
cat /opt/terminal-mcp/current/RELEASE_META.json
pid="$(systemctl show terminal-mcp.service -p MainPID --value)"
test "$pid" -gt 0
readlink -f "/proc/$pid/exe"
tr '\0' ' ' < "/proc/$pid/cmdline"; printf '\n'
```

Independently query existing authorized `/health/live` endpoint and the public MCP health tool using **the host's actual configured URL** (do not guess port); retain HTTP code, `ok`, storage/components and `version/release`. Compare `RELEASE_META.json` against the **active process** and frozen manifest: `commit_sha` is only the **first seven characters** of the approved full SHA, alongside `release_id`, `version`, `schema_fingerprint` and `code_fingerprint`. Prove the 40-character SHA independently through a file-by-file SHA-256 comparison of installed Python sources against a `git archive` of the approved commit (only the deliberate build-time `version.py` stamp may differ). A seven-character metadata value alone is insufficient provenance. Never use only `git rev-parse` of the historical checkout or only `version`. Save per-host UTC evidence `pre-` and `post-` under a secure operator artifact directory.

Release order *after a fresh explicit authorization*: (1) same-SHA isolated pair acceptance and backup+rollback compatibility check; (2) one approved FirstByte canary, verify local command receipts, local message 24h semantics, Task, retained history/SQLite and Events→dot; (3) only after accepted canary and confirmed mixed-version Mesh safety, second BacLOUD canary; (4) both host metadata/source/process/health match approved manifest, then isolated same-version Mesh 07–09; (5) release owner + independent QA sign off STAB-11. Stop progression on ambiguity, lost receipts, transport duplicate, false-green health, history mismatch, expired identity resurrection or changing SHA.

**Rollback**: stop further rollout and preserve diagnostics. Release owner inspects schema compatibility and preverified backup; revert *only executable/release pointer* to a previously validated compatible build, recheck PID/metadata/health/command-read/history and Mesh membership. **Never restore/overwrite authoritative working SQLite, downgrade schema, alter routing/firewall/WireGuard, or replay historical peer-message outbox automatically.** Because local messaging disables inter-server dispatch and may add schema24, an ordinary pre-cutover binary may restart old peer-message delivery: use an approved compatible *dispatch-disabled* rollback build or block rollback/deployment until available. Any incompatible on-disk migration requires a separate recovery decision, not a blind rollback.

## 6. Status at checkpoint / ownership

- Coordinator: Quebec-2 dot confirmed Uniform-2 sole release-gate ownership; source Canonical **8ea0a40** at assignment. On 8ea0a40 prep gate 2026-10-10 19:09:53–19:11:12 UTC: **255 passed, 1 deprecation warning, 0 failed, exit0 in 77.04s**, 23 existing modules, helper Python 3.13.5/SQLite 3.46.1, clean before/after. Negative final-mode missing-test and stale-SHA checks exited 2 (blocked). Logs: `/tmp/tg-u2-1010/` and `/tmp/tg-u2-negative/`.
- Local messaging + SQLite developer candidate **ea107d5** (Oscar-2), source targeted **197 PASS**, 28 health PASS and 40 repeat health checks; full exact suite and Romeo-2 independent QA were pending at latest handoff. **Not yet final integrated source SHA.**
- Read-only live MCP health at 2026-10-10 ~19:13 UTC: **FirstByte and BacLOUD** both report `0.20.9`, `status=healthy`, `storage=ok`, OAuth, healthy terminal/workflow/Mesh and peer components. This is connectivity/health evidence only; the historical source manifest (0ac13c8) and future final SHA still require separate process/file verification.
- MCP Events developer candidate observed at `0bcd322` on Papa-2's worktree; Sierra-2 independent QA and dot callback acceptance still pending.
- STAB-02/03 triple-HTTP-request incident attribution/lost-response contract remains a release decision and cannot be hidden by passing independent identical-command tests.
- Prior evidence E10–E13 shows source integration or historical scoped QA, **not** fresh real OAuth, same-SHA physical Mesh lifecycle, STAB-10 10min/500ops/3 restart or final release approval.
- One frozen full-suite belongs to Oscar-2 and independent QA; Uniform-2 runs only this critical targeted runner. No live deploy, network, backup restore, production DB or credential changes in preparation.

**Decision rule:** `source_gate=PASS` + independently approved exact full suite + zero open critical P0 + STAB-06–10 isolated/live evidence + signed real OAuth/Events-to-dot acceptance + per-host manifest/backup/rollback checks + explicit release authorization → canary eligible. Missing any required source or live proof → **BLOCKED / INCOMPLETE** with owner and exact next action, not a stable verdict.

### Deferred risk: STAB-02/03 — duplicate command execution (2026-10-10)

The observed triple execution corresponded to three separate HTTP requests. The caller-side retry trigger and the point where response/acknowledgement was lost remain unproven. A repeated request may execute a side-effecting command more than once. This candidate preserves the existing independent-command contract; it deliberately adds neither an invocation token nor request-level deduplication.

Per the 2026-10-10 operator decision, STAB-02/03 are deferred and do not block this release. Keep their diagnostic evidence; publish this risk to rollout operators and require separate stabilization before claiming exactly-once behavior.
