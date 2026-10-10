# TMCP-03 / RUN-01 — independent OAuth lineage acceptance

**Verdict: NON_BLOCKING for the pinned isolated implementation candidate.**
B1 PASS / B2 PASS / B3 PASS / B4 PASS, native SQLite compatibility PASS.
This is source-level QA acceptance only. **Live integration, Canonical promotion and deployment remain HOLD pending separate approval and live gates.**

## Identity / provenance

- Reviewer: Charlie-3, Secondary, separately bound legacy session; display suffix TMCP-03 OAuth Independent QA.
- Session ref: ws__d0SI5lseXwiq1TGMTc0_6iw; hard stop 2026-10-10 23:08:57 UTC.
- Coordinator: Zulu-2 (dot), renewed ref ws_b3mltoFumqgPbUlDhr_WJ8OZ, acknowledged exclusive OAuth QA ownership. This is NOT prior Charlie-3 Events and is separate from Bravo-3 generator.
- Source repository: U-ruRu/terminal-mcp.
- Pinned verified local/remote source candidate: 6849760b5109efc576f019835ea5109b976b6c09, origin/task/TMCP-OAUTH-CONTINUITY-zulu2-20261010.
- Candidate parent/base compared: 16b9d4dc8b14100196f35c80198d17e6d8a761f8.
- Previous independent QA report: /workspace/terminal-mcp-artifacts/delta3-oauth-b1-b4-qa-20261010/REPORT.md; independently verified previous QA remote SHA 72edebdaa4c55335e77497f59afe87f8794e1f9d, origin/qa/TMCP-OAUTH-B1-B4-delta3-20261010.
- Own isolated worktree: /workspace/terminal-mcp-oauth-lineage-qa-charlie3-20261010.
- Own QA branch: qa/TMCP-OAUTH-CONTINUITY-charlie3-20261010.
- Original Zulu-2 HANDOFF.md/CHECKPOINT.md checked against CHECKSUMS.sha256: both OK.
- No production database, auth material, environment settings, running service, Canonical or other contributor's checkout was changed. All fixtures generate synthetic identities under pytest temporary directories.

## Static source review

1. B1: intact AuthFoundation v3 version/table/security checks and existing access-key enforcement before transfer/backup; missing protected authority cannot be automatically fabricated.
2. B2: six OAuth/console relations, index and full SQL DDL comparison, including UNIQUE client_id and AUTOINCREMENT, are verified before copying; neither merge nor overwrite of existing destination is performed.
3. B3: external auth.sqlite3.identity-anchor created with O_CREAT|O_EXCL, mode 0600, fsync file and directory. New migration marker contains nonce and HMAC-SHA256 keyed with existing verifier key. Startup/inspect/migration/clean call complete protected authority + marker/anchor/key lineage verification. Existing marker with old/invalid DDL cannot silently be treated as isolated. Fresh bootstrap is limited to no preexisting protected auth/key/anchor and an absent or empty constructor-created working mesh. Fail-closed classifications are explicit. A complete pending anchor after an interrupted offline forward is reused only with original legacy source; corrupt/partial witness is never silently reset.
4. B4: forward and reverse retain SQLite AUTOINCREMENT high-water from sqlite_sequence, even if audit rows were deleted.
5. Backup/clean: authoritative inventory includes required independent anchor; quiesced snapshot copies and checksums it; clean quarantine writes an independent anchor snapshot before working-file renames. Runbook treats auth DB + verifier key + anchor + config as a recovery unit, and warns about stale but authentic older snapshots.

Files reviewed: src/terminal_mcp/auth/identity_migration.py, src/terminal_mcp/backup.py, src/terminal_mcp/app.py, docs/oauth-identity-isolation-runbook.md, original and revised QA tests. Source-vs-base git diff --check exit 0; independent QA Ruff lint exit 0. No source implementation edited by reviewer.

## Independent execution results (no full test suite)

Environment: Linux 6.12.101+deb13-amd64 x86_64; git 2.47.3; Python 3.13.5; pytest 8.4.2; SQLite 3.46.1 system and 3.53.4 native preload. Tests run with PYTHONPATH=$W/src:$W/tests:$W/qa in own checkout and fixtures under pytest tmp_path.

| Receipt / command hash | Scope | SQLite | Exit | Result |
|---|---|---|---|---|
| e7f246bf | Original independent Delta-3 QA cases replayed on pinned new SHA, changed diagnostic-status assertions and 2 former strict-XFAIL now obligatory | 3.46.1 | 0 | 29 PASS |
| 2e7402d7 | Initial new QA fixture run (incorrect synthetic Fleet flags in backup snapshot test) | 3.46.1 | 1 | 12 PASS / 1 fixture FAIL; fixed locally, not a product failure |
| c140a5fa | Corrected targeted snapshot test | 3.46.1 | 0 | 1 PASS |
| b5e35416 | 13 newly-authored independent anchor/SIGKILL/recovery/snapshot tests | 3.46.1 | 0 | 13 PASS |
| 96c5705c | Combined independent suite before 3 added partial-failure tests | 3.53.4 | 0 | 42 PASS |
| db7e8161 | Three new partial-failure/inventory tests | 3.46.1 | 0 | 3 PASS |
| 88c46bdd | Full targeted independent suite including partial failures (before lint-only formatting) | 3.46.1 | 0 | **45 PASS** |
| 28f33c10 | Full targeted independent suite including partial failures after QA formatting | 3.53.4 | 0 | **45 PASS**, 43.85s |
| 22d4cd8c | Same final QA files after formatting | 3.46.1 | 0 | **45 PASS**, 38.84s |
| 38469241 | Static candidate diff --check and QA Ruff lint on pinned implementation | source | 0 | PASS |

Final log filenames are under /workspace/terminal-mcp-artifacts/charlie3-oauth-lineage-qa-20261010:
independent-delta-replay.log, independent-anchor-native346-final.log, independent-all-sqlite346-final.log, independent-all-native353.log, independent-all-sqlite353-final.log, independent-all-sqlite346-postformat.log, matching .exit files; environment-static.log, ruff-qa-final.log and source-diff-check.log. Source test files are qa/test_oauth_independent_charlie3.py (adapted independent Delta-3) and qa/test_oauth_anchor_charlie3.py (new independent assertions).

Run pattern:

    cd /workspace/terminal-mcp-oauth-lineage-qa-charlie3-20261010
    export PYTHONPATH="$PWD/src:$PWD/tests:$PWD/qa"
    /workspace/terminal-mcp/.venv-test/bin/python -m pytest -q \
      qa/test_oauth_independent_charlie3.py qa/test_oauth_anchor_charlie3.py -ra
    LD_PRELOAD=/workspace/terminal-mcp-evidence/romeo2-20261006/native-sqlite/libsqlite3.so.0 \
      /workspace/terminal-mcp/.venv-test/bin/python -m pytest -q \
      qa/test_oauth_independent_charlie3.py qa/test_oauth_anchor_charlie3.py -ra

Only the above focused suites were run; developer's 179 related/47 native/29 replay are separately authored evidence, not claimed as this acceptance.

## Criterion matrix

| Criterion | Verdict | Independent evidence |
|---|---|---|
| B1 protected AuthFoundation/file/key/version missing or malformed | PASS | Missing/empty protected DB, invalid Foundation version/security singleton/principals/key fail before copying, six legacy rows remain digest-equal |
| B2 exact target SQL schema, device UNIQUE, retention indexes, defaults and AUTOINCREMENT | PASS | Required schema mismatch and extra unique index fail closed; valid empty destination still forwards |
| B3 wrong fully-initialized auth store (different key AND SAME verifier key) | PASS | Foreign protected authority with new valid marker and distinct anchor refuses inspect-as-isolated, startup, migration and clean; original auth, key and anchor restored from paired trusted synthetic snapshot recover clients/devices |
| B3 missing, swapped, malformed, loose-mode external anchor, wrong verifier key/marker proof | PASS | All fail closed; no quarantined/moved working data; correct triplet restores |
| B3 legitimate pristine bootstrap / repeated startup / old mesh-history rejection | PASS | Synthetic TestClient health and marker; preexisting mesh history not mistaken for new install |
| B3 interrupted offline forward and pending anchor retry | PASS | SIGKILL in CHILD process after anchor fsync before commit; SQLite legacy source unchanged; same pending nonce reused for successful offline retry |
| B3 corrupt pending anchor and partial fresh-install witness | PASS (fail closed) | No silent replacement or new identity adoption; operator recovery required |
| B4 audit AUTOINCREMENT high-water forward/reverse | PASS | Deleted tail/all rows preserved; next identity monotonically increases |
| Snapshot inventory/quiescence/backup integrity | PASS | Anchor marked secret+required, present in protected snapshot; missing anchor remains required; unquiesced snapshot refused; tampered snapshot refused |
| Paired snapshot restore | PASS | Synthetic auth+key+anchor checked after relocation, tokens/devices present; lost anchor refusal and trusted restore recovery |
| Normal transfer, clean, restart and OAuth retention | PASS | OAuth registration/device links/expiry/refresh rotation/used-token rejection and App lifespan restart preserved |
| Mid-clean file-rename failure and anchor-backup failure | PASS | Synthetic injected failures preserve auth/main/output and permit safe retry |
| Real ChatGPT, Android device, actual remote OAuth endpoints, live DB restore | NOT TESTED | Strict synthetic-only boundary, requires separate authorized live gate |
| Whole-history rollback or newer-state freshness detection | NOT TESTED (out of scope) | By contract, valid older snapshot may retain proof yet contain stale grants/revocations |

## Partial failure and runbook assessment

No source-blocking loss or untrusted-identity acceptance found. The external witness is outside SQLite's atomic transaction by design. A crash after successfully persisting the anchor but before commit leaves a pending witness and intact legacy source: approved OFFLINE retry reuses it. A truncated/corrupted witness stops migration and startup; manual reviewed recovery is required. Failed clean operations can leave a partial quarantine directory containing backup members; they do not transfer authority. Use a new quarantine destination after validating source.

**Runbook clarification recommended (NON_BLOCKING to isolated source QA):**
- Require and record an explicit read-only inspect = isolated before taking an authoritative post-migration snapshot, and AGAIN after recovering the auth+key+anchor+config quartet, prior to startup/clean. Snapshot checksum validation by itself does not prove semantic DB/key/anchor consistency.
- Add a specific operator decision path for a valid pending anchor with legacy tables intact versus malformed/partial anchor, including interrupted FIRST bootstrap. Never auto-remove the witness or key merely to force fresh install.
- Require operator review of snapshot freshness, token revocations/rotations and current authoritative records. A matching historical nonce/HMAC does not establish newer authorization state. Mutation-synchronized anti-rollback is explicitly outside the contract.

## Live gates and release

**NON_BLOCKING for integrating the source patch after coordinator approval only.**
Live FirstByte/BacLOUD acceptance, real ChatGPT refresh/device continuity, operator backups, service-stop/quiescence and release cutover, Canonical promotion, and any production deployment were **NOT TESTED and NOT AUTHORIZED** here. Until independently approved they remain HOLD; this review must not be presented as production go-live certification.

No actual credentials or productive services were contacted. QA branch publication and post-push remote SHA are recorded in the external CHECKPOINT/HANDOFF.
