# Native Access Mesh trust — continuation checkpoint 2026-10-12

* Working tree: `/workspace/terminal-mcp-access-trust-cont-20261012`
* Branch: `task/native-access-trust-cont-20261012`
* Base: `0cbd9c38a0f6bb6ba23f87849ed579866b2d7ace`
* Preserved prior WIP: `fab7afeb13e79e1b9bb8e8fbe2937beb37593251` and exact unstaged patch under `/workspace/terminal-mcp-evidence/lima5-handoff-20261012/`
* Four live actual controlled DBs verify stable `mesh-a2411f8d76d65643`, rev4 all; database path warning: BacLOUD/FirstByte use `fleet-control-fb-bac-v1.sqlite3` not old `fleet-control.sqlite3`.
* Empty static Native peer lists; Main Access proof HMAC key differs. NO LIVE CHANGES/DEPLOY/RESTART.
* Added versioned authenticated Native Access proof challenge, peer proof verification, read-only GET status `control.native_access_trust`, tests. Juliett must gate APK Join on readiness, not existing green membership.
* Targeted tests 13 PASSED using `PYTHONPATH=src /tmp/tmcp-qa-papa21-venv/bin/python -m pytest -q tests/test_managed_access_trust_enrollment.py tests/test_access_mesh_trusted_fleet_peer_health.py tests/test_access_mesh_replication_http.py tests/test_access_mesh_managed_proof_gate.py`.
* Blocker: broader regression one failed `test_applied_mesh_replica_and_detach_keep_current_snapshot` (synthetic managed peer issue hangs/timeouts); isolate original `fab7afe` versus this branch and repair without weakening security.
* Live sign-off pending dot/user; no manual apply. Next action: send commit to Juliett-5, complete APK readiness/partial journal, verify proof migration, full tests, and only then accepted fleet rollout + BacLOUD->FirstByte/Tokyo/Main attach.
* Exact test and live recovery commands in `REPORT.md`. Review actual process env/DB path first; never overwrite auth DB or active work sessions.

## Lima-5 continuation checkpoint, 2026-10-12 14:53 UTC

New worktree /workspace/terminal-mcp-native-trust-finish-lima5, branch task/native-access-trust-finish-lima5-20261012, parent 6d6593d0a667734139b4afa5d033a56a66b15e4a. Generic authority-derived managed group proof with encrypted local historical proof archive added; no Main-specific behavior. Baseline Legacy/Persistent regression fixed: originally fab7afe timeout after 27s, now target passes. 66 module tests pass plus independent 6 checks. No live writes/deployment.

Handoff to Juliett-5: cherry-pick candidate after integration review, gate existing APK Join on native_access_trust.ready and all nodes' convergence, durable retry of partial Join, full release suite and real Android E2E. Sole deploy owner Juliett, no competing releases. User later requested four standalone nodes with auth/RKB retained, Secondary separate, user performs Join personally. No reset until action-time manifest approval; no real Access -> remote Exec pass yet. Full commands and restore plan in REPORT.md.

## Security addendum, 15:00 UTC

Managed Join pending proof rejects REMOTE attach; old local Access slots retain local attach through Join/Leave/restart. Authority changes immediately invalidate former active proof scope. Test suites: 19 + 30 + prior 66 passed. No deploy/reset/real Android run; release integration and all live tests remain with Juliett-5 and subsequent user Join.
