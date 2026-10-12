# Native Access trust — continuation report (2026-10-12, Lima-5)

## Preserved starting point

* Base release SHA: `0cbd9c38a0f6bb6ba23f87849ed579866b2d7ace` (0.26.0).
* Previous Lima-5 WIP commit: `fab7afeb13e79e1b9bb8e8fbe2937beb37593251`.
* The previous working tree carried an uncommitted one-line test change. Exact binary patch, sha256, state and original reports are preserved under `/workspace/terminal-mcp-evidence/lima5-handoff-20261012/`.
* Continuation is isolated in `/workspace/terminal-mcp-access-trust-cont-20261012` / `task/native-access-trust-cont-20261012`; previous tree untouched.

## Live facts: validated with actual running PID and DB path

Read-only inspection performed at **2026-10-12 14:09 UTC**. Exact reproducible read-only probes:
```sh
# Runs on Secondary; prints no secrets.
for h in bacloud firstbyte tokyo solvenger-main; do
  ssh -o BatchMode=yes "$h" python3 - < /workspace/terminal-mcp-evidence/lima5-handoff-20261012/runtime_paths.py
done
```

* Running BacLOUD PID=2785312, FirstByte PID=4152401, Tokyo PID=578659, Main PID=2867242; all have instance IDs matching expected names.
* Critical path detail: BacLOUD and FirstByte use **`/var/lib/terminal-mcp/fleet-control-fb-bac-v1.sqlite3`** via the live process environment. The other `fleet-control.sqlite3` on these two hosts is a **stale October 10 DB**, not the running topology. Tokyo/Main use `/var/lib/terminal-mcp/fleet-control.sqlite3`.
* ALL FOUR actual Mesh DBs agree on `mesh-a2411f8d76d65643`, four active members BacLOUD, FirstByte, Tokyo, Main, desired/applied topology rev4, no `last_error`. Secondary is not in this Mesh. No runtime topology drift. The earlier stale-DB alert was withdrawn with dot and Juliett-5.
* `ACCESS_MESH_ENABLED=true`; static `ACCESS_MESH_PEERS_JSON=[]`, `FLEET_PEERS_JSON=[]` on all four.
* Access proof secret SHA-256 (first 12 hex, NOT the secrets themselves): BacLOUD/FirstByte/Tokyo `40a57965d8ff`; Main `0d3ac02ce359`. This is a true mismatch and blocks verified Native Access readiness.
* In the previous session a fresh BacLOUD-issued number attached locally, while FirstByte/Tokyo/Main returned `invalid_session_number`. Claims remained local to BacLOUD; no proof of a successful cross-node attach yet. Earlier number has expired and must not be reused.
* Previous Lima-5 created per-host sensitive backups under `/var/backups/terminal-mcp/native-access-20261012-lima5/`: `terminal-mcp.env` and SQLite API backup of fleet DB. DO NOT copy or print those secrets; before live changes verify new per-host backup freshness, exact runtime DB paths and unchanged OAuth/slot state.
* No runtime trust/env changes, service restarts, database resets, OAuth or pairing changes were performed in this continuation.

## Implementation candidate

The previous commit (`fab7afe`) derives Native Access issuer allowlists from locally committed verified Fleet Control membership and switches bearer peer material to the authenticated managed configuration. It revokes a former remote issuer on leave/change authority/transfer. Bootstrap autonomous mode retains pinned credentials.

Continuation adds:
1. Authenticated internal `POST /internal/fleet/access-mesh/proof` with strictly validated random nonce and HMAC over versioned sender/receiver context. No raw proof secret or private signing key crosses the wire. Unknown peer, bad token, malformed challenge rejected.
2. `AccessMeshReplication.verify_peer_proof` validates proof-key agreement before synchronizing any managed peer. Different keys mark `proof_mismatch`, transport errors `unavailable`. Cache is invalidated on peer/public-key/token/origin/local-token change and on restart; bounded recheck every 300s.
3. Operator GET `/actions/fleet/control` now exposes `control.native_access_trust` with `state: autonomous|pending|ready`, `ready`, peer statuses and missing transports. **The APK must explicitly gate its Join success on `native_access_trust.ready===true` after all desired/applied membership revisions converge.**
4. Isolated proof endpoint and mismatch/recovery/restart tests. Any member's incorrect proof cannot be called ready.

### Tests, exact command and limitations

```sh
cd /workspace/terminal-mcp-access-trust-cont-20261012
PYTHONPATH=src /tmp/tmcp-qa-papa21-venv/bin/python -m pytest -q \
 tests/test_managed_access_trust_enrollment.py \
 tests/test_access_mesh_trusted_fleet_peer_health.py \
 tests/test_access_mesh_replication_http.py \
 tests/test_access_mesh_managed_proof_gate.py
# 13 passed in 5.44s (2026-10-12 14:13 UTC)
git diff --check
/tmp/tmcp-qa-papa21-venv/bin/python -m compileall -q src/terminal_mcp
```

A broader isolated regression with `tests/test_access_slot_inheritance_http.py` alongside the first three modules produced **19 passed, 1 failed**, `test_applied_mesh_replica_and_detach_keep_current_snapshot`: synthetic managed member peer cannot confirm its session issuance within the request timeout. This failure may originate in the previous managed membership change (`fab7afe`) rather than the new proof endpoint. It is **NOT accepted** as a green suite. Must fix contract without making global issue unconfirmed or breaking legitimate Legacy/Persistent behavior.

NOT DONE: live BacLOUD Access -> all Exec attach, Android E2E, proof migration/enrollment on Main, API Join readiness gate in existing APK screen, persisted per-node trust-apply receipts/errors for partial failure, larger regression, restart E2E, OAuth/pairing validation on actual device. Until these are accepted, **no production deploy or completion claim**.

## Production plan and approval gate

1. User/dot confirms explicitly: authority BacLOUD, target participants BacLOUD/FirstByte/Tokyo/Main only, no Secondary, maintenance window, and protected Access-proof agreement method for Main. Stop concurrent topology mutations during window.
2. On all four hosts read PID/env, exact managed DB, topology and keys/fingerprints, active work sessions and auth/pairing inventory. Backup actual env and SQLite files via SQLite online backup under a new timestamp; keep prior backups. Do not copy Ed25519 signing private keys between nodes.
3. Release integrator Juliett-5 merges and verifies code, Android Join/proof readiness gating and recovery for missing proof; use a single accepted candidate and documented deploy, rolling one application service at a time. Main's proof must be securely enrolled using the authenticated protected mechanism and explicit authorization; no ad hoc keys in logs.
4. Check `control.native_access_trust.ready` for each node; mismatch/unavailable/unknown remain pending, not success. Verify authenticated peer identity and Mesh policy while releasing.
5. Only when ready, obtain one **fresh** BacLOUD Access session and immediately attach SAME number to BacLOUD/FirstByte/Tokyo/Main Exec. Record exact UTC and success/error per node, repeat after a controlled restart with a fresh valid number.
6. On any failure stop, inspect committed stage and logs. Roll back application release per documented version procedure; restore env only after safeguarding any intervening OAuth/pairing changes; restore SQLite only offline after explicit approval. Never reset active session/job databases or clone per-node private signing keys.

## Coordination

* dot current Secondary identity November-5, BacLOUD identity Foxtrot-31 (verified 13:55 UTC); exact path correction sent to November-5 and Juliett-5.
* Juliett-5 owns existing APK/API and deployment integration; Kilo-5 owns recovery logic. This candidate is a separate backend branch, not a competing release.

## 2026-10-12 14:53 UTC — automatic Native Access enrollment continuation

Historical regression resolved: baseline 0cbd9c3 passes test_applied_mesh_replica_and_detach_keep_current_snapshot in 5.93s; previous fab7afe fails after 27s. Dynamic managed peers entered global session-number reservation before Access trust was established, blocking isolated local Legacy/Persistent issuance. Fix: local-only issuance while native trust remains pending; full cross-node reservation/claim forwarding requires proof verification, and APK Join remains pending.

Generic enrollment, not special-cased for Main: after signed/authenticated managed Fleet Control membership applies, each authorized member derives its Mesh-scoped Native Access group key from the already-replicated authority ingress credential plus mesh_id/authority_id (domain-separated HMAC). No Fleet signing private key is exported; no manual Access environment edit on newly joined nodes. Invalid/missing authority material results in pending/error rather than success.

Preservation: the node-local bootstrap proof stays immutable for old local PINs and sealed receipts. Previously used managed proof keys are encrypted in access_mesh_local_proof_history in existing Access SQLite and accepted ONLY for node-local historical slots. Removed remote issuers cannot use archived proofs. Join retry is idempotent, leave clears active managed proof while archived local sessions survive restart. Rotating the node-local bootstrap proof itself requires a deliberate archive migration.

Validation: 66 tests passed (one external dependency warning) across access_slot_inheritance_http, managed_proof_auto_enrollment, managed_access_trust_enrollment, access_mesh_managed_proof_gate, access_mesh_replication_http, access_mesh_trusted_fleet_peer_health, access_mesh_session_restart_contract, access_mesh_native_lifecycle, access_mesh_grants, fleet_control_hardening in 38.97s (14:46 UTC). Follow-up 6 tests passed in 7.57s (14:49 UTC), covering signed control-plane enrollment/retry, two-node real HTTP authority issue -> remote attach with unlike local bootstrap proofs, revocation, leave/restart and invalid proof. Source compileall and git diff --check passed.

Reproduce on Secondary:
  cd /workspace/terminal-mcp-native-trust-finish-lima5
  PYTHONPATH=src /tmp/tmcp-qa-papa21-venv/bin/python -m pytest -q tests/test_access_slot_inheritance_http.py tests/test_managed_proof_auto_enrollment.py tests/test_managed_access_trust_enrollment.py tests/test_access_mesh_managed_proof_gate.py tests/test_access_mesh_replication_http.py tests/test_access_mesh_trusted_fleet_peer_health.py tests/test_access_mesh_session_restart_contract.py tests/test_access_mesh_native_lifecycle.py tests/test_access_mesh_grants.py tests/test_fleet_control_hardening.py

Release and live not accepted: Juliett-5 owns existing APK Join UI, success gating on control.native_access_trust.ready, durable partial retry/reconcile, release integration and the sole deployment. Do not claim success from membership-only applied revisions. No live server, trust, auth/RKB, data, pairing or service was changed here. Still unverified: full CI, Android device E2E, multi-node status acknowledgments, real BacLOUD Access PIN -> remote Exec attach and restart retest.

Operator constraint from dot at 14:45 UTC: user wants an accepted update on FOUR STANDALONE servers preserving OAuth/auth and RKB, leaves Secondary untouched, and will personally join all servers including Main using APK. Do not join Main, reset anything, or deploy without Juliett's precise approval-time file deletion/preservation manifest. The candidate branch is not an accepted release.
