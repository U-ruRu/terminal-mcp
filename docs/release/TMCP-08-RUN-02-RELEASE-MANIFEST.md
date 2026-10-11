# TMCP-08 / RUN-02 — integrated correction candidate

**Status: INTEGRATED CANDIDATE; FULL SUITE HOLD; DEPLOY / CANARY NO-GO.**
The final commit SHA is attested externally in the immutable manifest:
 /workspace/terminal-mcp-artifacts/tmcp08-run02-foxtrot3-20261011/RELEASE-MANIFEST-IMMUTABLE.json
A commit cannot embed its own final hash. Targeted PASS is not full-suite PASS.

## Provenance

Repository U-ruRu/terminal-mcp, integration branch
integration/TMCP-08-RUN-02-foxtrot3-20261011.

- Exact starting base: 9c23bd6762fc1b06af6fce963befe6a18835c546.
- OAuth fixture source: c6933cc8cb9c47b000fa37bd06667ea5deb05fd1,
  task/TMCP-OAUTH-MARKER7-BRAVO3-20261011.
- Approved additive output schema snapshots:
  7a31218d6cb8e9d0a26eb1571e0c24a83a28dfe9,
  task/TMCP-SCHEMAHASH-DELTA3-20261010.
- Both source SHAs remain ancestors of the integration HEAD through two
  independent no-ff merge commits. No source was rewritten.
- Compared to base, only 3 OAuth synthetic test modules, 3 reviewed schema
  JSON snapshots, a TMCP-10 handoff and these release docs changed. No
  production Python source, OAuth migration, runtime, Canonical, config
  or permissions changed by this integration.

## Preserved previous failure

TMCP-08 RUN-01 common pytest against base 9c23bd6: 2311 tests,
2301 passed, 10 failed, zero skipped, exit 1, one Starlette warning.
All original receipts remain unchanged under
/workspace/terminal-mcp-artifacts/tmcp07-release-bravo3-20261010/FULL_SUITE.{log,junit.xml,exit,FAILED,sha}
and previous triage under
/workspace/terminal-mcp-artifacts/tmcp08-run01-bravo3-20261010/REPORT.md.
Do not overwrite or reclassify this run.

## Independent OAuth security acceptance (Foxtrot-3)

- Own detached checkout at exact OAuth source. Reviewed only three edited
  tests, no runtime/marker weakening. Explicit tmp main/auth/key/anchor,
  env-file disabled and isolated test env prefix.
- Verified author JUnit: seven original, ten fixture and 34 identity PASS.
- Independently executed all 10 fixture + 34 identity tests with scrubbed
  environment and poisoned synthetic foreign auth/main overrides:
  44 PASS, 0 FAIL, 0 SKIP, exit 0, JUnit saved.
- Independently created three disposable security probes: synthetic env and
  dotenv cannot redirect auth; guarded fake external SQLite/stat/open is
  untouched; missing migration marker blocks startup without resealing key
  or anchor. Three PASS, exit 0. Host secrets were not inspected.
- OAuth fixture verdict NON_BLOCKING. Separate WAL cross-file crash
  atomicity and real signed callback remain unproven.

## Independent additive schema acceptance (Foxtrot-3)

- Own detached historical checkout at bde0dec8bd0679c3cbda768104672de57956b6cf
  plus current integration checkout; never-started synthetic MCP apps.
- Calculated all seven legacy tool hashes and all three role manifests on
  BOTH versions; all equal their respective published JSON snapshots.
- Exactly four legacy changes, all additive: cmd and message outputSchema
  each adds optional MessageRecord.deliveries and closed optional
  MessageRecipientReceipt with eight optional fields. No existing schema
  property or required field modified. Other five legacy tools unchanged.
- Exactly six role-manifest runtime_output_digest values differ, with no
  input schema, planning, roles, endpoint or annotation changes.
- Independent field-level artifact:
  /workspace/terminal-mcp-artifacts/tmcp08-run02-foxtrot3-20261011/INDEPENDENT_SCHEMA_FIELD_DIFF.json
- Narrow schema verdict NON_BLOCKING. TMCP-11/RUN-01 remains a separate
  assigned independent review and formal release gate.

## Combined targeted acceptance

Receipts in /workspace/terminal-mcp-artifacts/tmcp08-run02-foxtrot3-20261011/:
- All ten exact RUN-01 failed nodeids: 10 PASS, no skips/xfail.
- Six affected message/compact/output test files: 156 PASS, no skips.
- scripts/build_compact_catalogs.py --check: PASS for all ten generated
  artifacts at PYTHONHASHSEED 0, 1 and 8675309. Pre/post SHA256 inventories
  identical; no generated artifacts rewritten.
- Ruff check, Ruff format --check for 3 changed Python test files and
  git diff --check: PASS. Only existing TestClient/httpx deprecation warning.
- Exact commands, start/finish UTC, JUnit and genuine exit receipts preserved
  externally; final published SHA proof follows last docs commit.

## HOLD, FirstByte/BacLOUD restrictions

**FULL COMMON SUITE NOT RUN IN TMCP-08/RUN-02.** Dot reserves exactly one
run until independent TMCP-11 verdict, clean immutable published final SHA,
a new reviewed runner, exclusive process/receipt checks and sufficient
session time. The older prepared runner pins failed SHA 9c23bd6 and its
pgrep/grep self-match guard falsely reported another pytest: do not reuse
that original runner unchanged.

Canary FirstByte/BacLOUD both NOT DEPLOYED and NO-GO until separately
approved full-suite exit 0, real dot signed callback/ChatGPT connector A/B,
host-specific OAuth/key/anchor continuity, offline backups, rollback and
sequential operator acceptance per docs/release/TMCP-07-CANARY-RUNBOOK.md.
Cross-file crash atomicity and SIGKILL recovery are not established.
No live runtime, credentials, Canonical, access settings or Solvinger staging
load were changed by this task.
