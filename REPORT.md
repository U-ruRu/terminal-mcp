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
