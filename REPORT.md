# Native Access Mesh Trust: Lima-5
Date: 2026-10-12. Base SHA: 0cbd9c38a0f6bb6ba23f87849ed579866b2d7ace.

## Current production (read only)
- BacLOUD, FirstByte, Tokyo, Main terminal-mcp.service active at initial inspection.
- Four nodes: ACCESS_MESH_ENABLED=true, ACCESS_MESH_PEERS_JSON=[], FLEET_PEERS_JSON=[].
- Fresh BacLOUD Access number: BacLOUD Exec attach succeeded; FirstByte, Tokyo, Main returned invalid_session_number.
- BacLOUD/FirstByte/Tokyo shared an Access proof-key fingerprint; Main's was different. Only hashes compared, never output any raw secret.
- Root cause: AccessMeshStore trust derived from local/static bootstrap only, and PinnedAccessMeshPeerAuth authenticates only bootstrap peers. Managed Fleet membership did not enroll Access issuers.
- Secure pre-change backups for EACH node: /var/backups/terminal-mcp/native-access-20261012-lima5/{terminal-mcp.env,fleet-control.sqlite3}. Created via cp -p and Python SQLite backup; 88–96 KiB DB copies; contains secrets, leave on node.
- Live trust configuration/restarts/deploy/OAuth/APK pairing and current slots: NOT CHANGED.

## Candidate code and limits
- Trust issuer set for Native Access is recalculated only from authenticated managed Fleet Control replica: local node, authority and non-detached members must all be in same Mesh.
- Managed peer authentication/replication uses managed ingress credentials, bootstrap pins remain for standalone mode.
- Reattach by old untrusted issuer is rejected. Leave, authority removal, Mesh transfer revoke prior allowed issuer IDs.
- Existing active local jobs are not terminated by this trust-set recalculation; future remote claim/attach is restricted. A fuller revocation policy and notification still need integration.
- Juliett-5 owns APK/API integration; this commit does not implement the full APK Join readiness transaction. Existing Mesh green may still be a FALSE success for Native Access.
- Main's different proof-key remains a live blocker. Coordinate protected migration; do not transfer signing private keys. Match secret scope under explicitly authenticated enrollment before rollout.

## Validation
- Compiled Python sources and isolated test module successfully with compileall.
- git diff --check successful.
- Added tests/test_managed_access_trust_enrollment.py for same-Mesh enrollment, bad authority, external node, leave, transfer, changed authority, bearer auth.
- pytest unavailable in checked interpreters; run in official build/test environment:
  PYTHONPATH=src python -m pytest -q tests/test_managed_access_trust_enrollment.py tests/test_access_mesh_trusted_fleet_peer_health.py tests/test_access_mesh_replication_http.py
- NOT YET VERIFIED: live FirstByte/Tokyo/Main attach success, OAuth and APK pairing E2E, Android UI, lost response/retry, restart, proof migration, partial failure, active-job continuation.

## Live recovery and release gate
1. Obtain explicit coordinated approval before changing four-node trust.
2. Preflight Mesh authority/revisions, peer signatures/identity, proof-key agreement, OAuth pairing preservation and all active sessions.
3. Install a tested accepted release only after Juliett-5 integration and tests; perform rolling enrollment + restart as required with safety gates.
4. Obtain ONE fresh BacLOUD Access session number; confirm SAME number attaches to BacLOUD, FirstByte, Tokyo, Main Exec. Check negative invalid issuer/proof, and restart.
5. If failure, stop enrollment, save current evidence and roll back accepted release; restore each node's protected env only after preserving newer OAuth/pairing modifications. Restore fleet DB only with maintenance window and SQLite backup API; never overwrite live DB. Do not touch user jobs or shared private signing keys.
6. Report success only after live Access -> Exec pass. Do not deploy this candidate directly.
