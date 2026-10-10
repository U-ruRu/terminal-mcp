# Terminal MCP OAuth isolation B1–B4 — independent QA (Delta-3)

**Verdict: BLOCKING — HOLD integration and deployment.** B1, B2 and B4 pass independent reproduction and positive controls. B3 has a confirmed data-continuity blocker: an unrelated, fully initialized but empty protected OAuth/Access database with valid schema and migration marker is accepted as authority for an existing installation. Its original identities disappear; startup and clean both proceed. No live services or real credentials were used.

## Provenance and boundaries

- Exact immutable implementation SHA: **16b9d4dc8b14100196f35c80198d17e6d8a761f8**.
- Published implementation ref: origin/task/TMCP-OAUTH-B1-B4-FIX-alpha3-20261010; git ls-remote and fetched FETCH_HEAD matched the complete SHA.
- Repository: https://github.com/U-ruRu/terminal-mcp.git. Private QA worktree: /workspace/terminal-mcp-oauth-b1-b4-qa-delta3-20261010. QA branch: qa/TMCP-OAUTH-B1-B4-delta3-20261010. QA-only scripts are in qa/; the implementation source is unchanged from the candidate.
- Isolated Secondary legacy session Delta-3, 2026-10-10 21:34:29–21:57:29 UTC. Coordinator X-ray-2 was succeeded by Foxtrot-3 (dot) at 21:40:46 UTC. Developer Alpha-3 and Foxtrot-3 received immediate B3 evidence; Foxtrot-3 explicitly acknowledged release HOLD.
- Read Alpha-3 HANDOFF, prior Zulu-2 REPORT, candidate docs/oauth-identity-isolation-runbook.md, migration source and FastAPI app startup wiring.
- All negative checks exclusively used temporary synthetic SQLite fixtures. No FirstByte/BacLOUD, canonical, live main/auth, signing credentials, production OAuth, or running services were altered.

## Acceptance verdict by criterion

| Criterion | Result | Evidence |
| --- | --- | --- |
| **B1 — missing protected authority** | **PASS** | Missing protected file, empty replacement, invalid Foundation user_version=321, missing verifier key, absent security singleton, missing principals table all reject forward before backup. Six old working relations retain equal row digests. Old missing-auth reproduction now rejects. |
| **B2 — full SQLite target schema** | **PASS** | Empty destination missing UNIQUE(client_id), required retention index, altered default or AUTOINCREMENT, or containing extra UNIQUE rejects without changes to legacy relations. A structurally correct empty target is accepted and retains UNIQUE(client_id). |
| **B3 — inspect, startup, clean continuity** | **BLOCKING** | New unrelated auth SQLite containing AuthFoundation v3, six valid OAuth/console tables and completed marker is accepted as an existing migrated node's protected authority while original key file remains untouched. Eight protected relation fingerprints change, original identities vanish, inspect reports isolated, startup returns HTTP 200, clean deletes working main. Details below. |
| **B4 — AUTOINCREMENT audit high-water** | **PASS** | Old repro source seq=3, copied seq=3, next insert ID=4. All-audit-rows-deleted and postcutover reverse scenarios preserve high-water and prevent reuse. |

### B3 blocking reproduction: protected authority provenance gap

Independent deterministic reproducer: **qa/repro_b3_wrong_instance.py**. Receipt: **87fb0813, exit0**, artifact **repro-b3-full-continuity.log**.

1. Initialize legacy working SQLite and preexisting AuthFoundation on isolated temp paths. Create synthetic OAuth registrations and refresh, paired Android device, AuthFoundation principal, client, grant and realm. Migrate normally. Verify all relations moved into existing protected auth without changing the Access verifier key.
2. Bootstrap a separate pristine Terminal MCP instance via real FastAPI TestClient startup. It creates an unrelated protected SQLite with AuthFoundation version 3, all six OAuth and console relations, and the completed marker version 1. It has no identities of the original installation.
3. Copy only this new auth SQLite over the first installation's migrated auth database. Retain the first installation's original .access-key file unchanged. This simulates accidental protected-store replacement with an otherwise contract-valid database.
4. Compare SHA-256 row fingerprints across auth_principals, auth_clients, auth_grants, auth_realms, oauth_clients, oauth_refresh_tokens, console_devices and console_pairing_audit: **all eight relations differ**. Previous principal/password validation, grant, OAuth client, refresh, and device lookup all fail. The prior verifier-key hash remains identical.
5. Guard accepts the replacement: assert_identity_isolated succeeds; inspect.status is isolated; the original application's HTTP health endpoint returns 200; clean_working_data returns cleaned and moves/removes the remaining working main.

**Violated invariant:** the protected auth store selected by startup, inspect and clean must be demonstrably continuous with the **same pre-existing authorized installation** and its AuthFoundation/OAuth/device identities. A schema-compatible database with marker version 1 is not sufficient evidence of that lineage because unrelated fresh installations receive the same marker. Existence of the old verifier-key file without verification of a binding to the auth database also does not prove authority. A mere row-count threshold is not a valid fix: legitimate fresh installations may have zero OAuth clients.

Required remediation is an authenticated, persistent lineage/provenance proof anchored outside the replaceable auth database that survives the documented working-data clean and supports correct new installs and verified recovery. Guard must refuse unrelated replacements before startup and before any working-data removal. Consider the related problem of replaying older valid same-node snapshots when designing continuity checks.

Independent minimal counterpart: **probe-auth-replacement.log**, receipt **b77a7d5b**, exit0. Current failing contract regression is recorded as strict expected failure (XFAIL) in qa/test_oauth_isolation_delta3.py. A second diagnostic XFAIL documents that inspect on pre-migration legacy main reports legacy-present even when auth is missing; forward migration correctly blocks this, so it has lower impact than the first B3 defect.

### Other positive and negative checks

- Six OAuth/console relations and all AuthFoundation principal/client/grant/realm rows retained with equal logical digests through valid forward and clean; verifier key fingerprint unchanged.
- Isolated FastAPI refresh, rotation, revoked token, expired code, device pairing and revocation behave as expected. 30-day refresh TTL (2,592,000 seconds) preserved. A separate OS-process restart and SIGKILL before ATTACH transaction commit are included in the re-run original Zulu acceptance suite.
- Idempotent forward creates no additional backup; interruptions after_backup and before_commit leave original source complete, followed by successful retry. Reverse transfers **current post-cutover** auth to an empty compatible working destination. The old Zulu suite also covered rename failure recovery, conflicts, and clean sidecars.
- Fresh install without prior main/auth/key creates a valid protected store and completed marker, and survives restart; deleted Access Mesh history is not mistaken for pristine installation.
- Old Zulu test z11 originally sets protected AuthFoundation user_version=321, which intentionally violates the v3 contract. That invalid case is explicitly confirmed to reject before backup and without losing old source. An adapted positive WAL test with valid protected auth version=3 and disposable working DB version=321 passes migration, row equality, snapshots and permissions. The z11 exclusion does not conceal a normal supported WAL path.
- Backups validated using SQLite quick_check; snapshot permissions 0600 and backup directory 0700.

## Independent execution receipts

| Suite | Outcome | SQLite | Receipt / external log |
| --- | --- | --- | --- |
| Related foundation/OAuth/pairing/HTTP/health tests | **122 passed**, exit0 | 3.46.1 | ee01f7ad / related-regressions.log |
| Independent earlier Zulu-2 acceptance replay | **18 passed, 1 deselected**, exit0 | 3.46.1 | 7cd73430 / zulu-independent-positive-replay.log |
| Native identity and pairing regression suite | **32 passed**, exit0 | **3.53.4** | ca6eed19 / native-identity-pairing.log |
| New Delta-3 independent targeted suite | **27 passed, 2 xfailed**, exit0 | 3.46.1 | 10abbc44 / delta3-independent-final.log |
| Same independent tests under native library | **27 passed, 2 xfailed**, exit0 | **3.53.4** | 3d2dbd8e / delta3-native-3.53.4-final.log |
| Original B1/B2/B4 regressions | safe failures and no ID reuse | 3.46.1 | 0225b0d8 / b1-b2-original-repro.log, b4-original-repro.log |
| B3 full continuity negative reproducer | **unsafe acceptance confirmed**, exit0 as defect reproducer | 3.46.1 | 87fb0813 / repro-b3-full-continuity.log |

All external logs and exit receipts: /workspace/terminal-mcp-artifacts/delta3-oauth-b1-b4-qa-20261010/.

Developer Alpha-3's own 169 / 38 / 18 PASS statements are separate; these numbers have not been represented as independently reproduced. Full repository tests and actual ChatGPT/Android live connectors were not exercised in this isolated QA lane. Pytest emitted only an existing Starlette/httpx deprecation warning. No real secrets were displayed or persisted in QA report files.

## Required action and release gate

1. **HOLD** OAuth isolation integration and live deployment; do not run clean on production.
2. Developer must fix B3 protected-store lineage/continuity verification, including inspect, read-only startup preflight and clean. A foreign fully initialized database with correct schema, marker and zero prior identities must fail closed while retaining all available original working data.
3. Also correct misleading inspect status for legacy main with missing protected auth.
4. Publish a new immutable developer SHA. Rerun both strict B3 expected-failure tests as ordinary passing tests, B1/B2/B4, valid WAL and invalid Foundation v321, crash/retry, current-state reverse and native SQLite. Remove XFAIL marks only after confirmed repair.
5. Integration and real client/device acceptance remain **separate** after a new independent PASS.

**FINAL: B1 PASS / B2 PASS / B3 BLOCKING / B4 PASS — RELEASE HOLD.**
