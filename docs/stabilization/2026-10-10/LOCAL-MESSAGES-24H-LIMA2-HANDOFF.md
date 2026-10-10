# Local messages 24h: Charlie-2 continuation / Lima-2 handoff

**Accepted source base:** `3f819a733294b38355ca8968a0a0527e8aeb59f7` (`integration/M3-functional-candidate`). **Historical Charlie-2 base:** `0ac13c838a5133cb43211e4adaad4bfaee24e8a2`. The exact candidate SHA is the tip of branch `task/TMCP-LOCAL-MSG-24H-lima2-20261010`; record it with `git rev-parse HEAD` after reviewing this file. Candidate prepared in a new isolated worktree; original Charlie-2 working tree and accepted integration worktree were not modified.

## Ownership and evidence preservation

- Started new **legacy** session on Secondary. Queried active recipients and the local coordinator status. Romeo was still listed as active when messaged, but no acknowledgement of current coordinator identity arrived before that session expired. No other local-message executor process was present and no local-message task claim was discovered. Another agent is independently working on SQLite health; none of its files or working tree were altered.
- Before applying anything, preserved **all 15 Charlie-2 changed files**, original untracked test, full binary diff, status, HEAD, per-file SHA256 and archive checksums. Preservation location is an operator handoff artifact on Secondary, communicated separately to dot. The source remains unchanged.
- The transferred Charlie-2 patch was applied cleanly to a new worktree from the exact accepted base and checkpointed independently before these fixes. No accepted branch or runtime changed.

## Implemented contract

- Node-local recipient discovery, send, inbox/history, ACK, reply, notify/ack/alert modes, task association, existing read pagination and public access policy. Each node treats local attachments independently, including one logical identity attached to multiple nodes; mere remote identity replication cannot make a recipient locally sendable.
- New message IDs use `<local-node>:localmsg:<token>`. All legacy `:meshmsg:` rows, including old inbound cross-node records, are intentionally ignored, return `message_not_found` on ACK/reply, and never block via the AccessMesh message obligation gate. No old message contents/author/created_at need migration. Existing messages and commands/tasks/sessions retain their original database rows unchanged.
- Local persisted delivery is distinct from reading. Repeated ACK leaves the first stored read time and produces no duplicate receipt event. Two deliberate replies remain distinct message rows, each retaining the parent's ID even after its parent expires. Alert is released only by appropriate reply (system expiry-alert ACK exception remains intact) or by TTL. One recipient cannot acknowledge another recipient's message.
- Exactly 24 hours from immutable `created_at`; reading/ACK/reply/restart does not renew it. Checks occur before normal reads and ACK, and in the operation gates, even if the physical sweep is delayed. Retention worker sweeps every 60 seconds (within 5-minute requirement while running), resumes after restart and deletes only expired *new* messages and their own dependent message rows; old message junk stays until an independent operator cleanup authorization.
- New sends never generate remote message or receipt jobs. Retired inbound peer message/receipt/recipients endpoints return `feature_disabled` with no message creation. Previously queued remote message/receipt jobs are marked terminal once with `delivery_stopped_local_cutover` and `terminal_at`; prior `attempts`, original IDs, and `acked_at` (including NULL) are unchanged, so stopped delivery is never falsely reported successful. Repeated startup and malformed old timestamps are harmless. The old message worker no longer retries jobs after restart.
- Cross-node agent *observation* uses a separate authenticated read-only presence endpoint. Replication of tasks, sessions, identity, commands and receipts remains unchanged.
- `access_mesh_messages` health probes actual local store creation/read/recipient handling inside a rolled-back SQLite savepoint, plus worker state. It does not require peer connectivity. SQLite main `SCHEMA_VERSION` is incremented from 23 to 24 because of additive terminal metadata columns, per `docs/release-versioning.md`. Public tool default `scope=local` is reflected in the generated AccessMesh schema baseline.

## Verification commands

Run from the exact candidate worktree on Secondary with installed test Python:

```bash
PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/python -m pytest -q
/workspace/terminal-mcp/.venv-test/bin/ruff check .
python3 scripts/check_repository_privacy.py --root .
git diff --check
git status --porcelain
```

Targeted tests that exercise the new behavior:

```bash
PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/python -m pytest -q tests/test_local_messages_24h_victor.py tests/test_access_mesh_messages_v2.py tests/test_access_mesh_identity_history_qa.py tests/test_access_mesh_alert_ack_contract.py tests/test_access_mesh_agent_observe_fleet.py
PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/python -m pytest -q tests/test_access_mesh_mcp_runtime.py tests/test_access_mesh_native_lifecycle.py tests/test_access_mesh_session_recovery_qa.py tests/test_task_mesh_policy_receipts_v2.py tests/test_access_mesh_metadata_contract.py
```

Preliminary local evidence: first imported Charlie-2 selected suite 38 PASS; hardened suite 41 PASS; broader AccessMesh/Task/metadata suites 75 PASS; version/TTL/schema selected 30 PASS; Ruff and privacy PASS; see separate operator log paths and final exact-SHA gate report. These are executor tests, **not independent QA**.

## Operator application and safe rollback

1. Coordinator/independent QA reviews **this exact Git SHA**, confirms the public contracts and schema manifest, full tests/privacy/Ruff/diff, read-time expiry and 24h+edge behavior, old outbox attempts stable across isolated restart and absence of message peer RPC. Reconcile against any newly accepted canonical descendants. Neither this branch nor its SQLite schema should be auto-merged without that review.
2. Separately approved deployment only: first preserve tested point-in-time SQLite backups and a restoration check, code/source SHA manifest and old pending message counts. Stage on one approved node. On startup the new schema 24 safely adds terminal markers while preserving IDs/ACK NULL/attempts; malformed old rows are *not* migrated or parsed for deployment. Verify public local cycle, local storage health and unaffected task/session/command replication before remaining nodes.
3. For rollback, **do not blindly reinstall an old binary**. Schema 24 intentionally makes a schema-23 binary fail its compatibility check. Downgrading DB schema manually could also wake old remote-message retries. Use a separately reviewed compatibility build with peer message transport kept disabled, or remain on the approved candidate while addressing defects. Never clear terminal job markers or reset attempt counters just to make a dashboard green.
4. Purging old message junk is **out of this change** and requires separate user approval, backup and operator procedure. Production databases, queues, services and runtime were not changed for this candidate.

## Remaining acceptance boundary

Full suite exact-SHA result and independent QA verdict must be attached by the coordinator. Legacy `PersistentFleetBridge` compatibility endpoints outside the AccessMesh message surface still warrant explicit review if an older non-AccessMesh connector remains part of the permitted production contract. No release/merge authorization is conveyed by this handoff.
