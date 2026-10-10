# TMCP-07 / RUN-01 — integrated release candidate manifest

Status: INTEGRATION CANDIDATE; no approval to deploy, change Canonical, or replace the running process.
Owner: TMCP-07 Bravo-3, Secondary legacy session ws_rHGjN8C1h-I5xJtyl8lLa7DN.
Coordinator: dot Zulu-2; common integration exclusivity confirmed 2026-10-10 23:08 UTC.

## Repository and exact inputs

- Repository: U-ruRu/terminal-mcp.
- Fixed integration base: bde0dec8bd0679c3cbda768104672de57956b6cf
  (integration/M3-functional-candidate; original local/remote equality verified).
- Sole writable branch: integration/TMCP-07-RUN-01-bravo3-20261010.
- Worktree: /workspace/terminal-mcp-tmcp07-integration-bravo3-20261010.
- OAuth identity continuity: 6849760b5109efc576f019835ea5109b976b6c09
  (task/TMCP-OAUTH-CONTINUITY-zulu2-20261010).
- OAuth independent QA lineage: 4c3fad0886967c82dc44566d75edd66c106a4671
  (qa/TMCP-OAUTH-CONTINUITY-charlie3-20261010; independent tests retained).
- Access Events subscription authorization: c8a04a21ce69689b1d237491844fedfa1187647b
  (task/TMCP-MCP-EVENTS-REVOKE-charlie3-20261010).
- Access Events independent Bravo-3 QA source:
  sha256 0758e572f17931b249934b90eef278f4e3c9a9c44a2e4fb494acdbc73da2c7ce,
  preserved as qa/test_mcp_events_qa_bravo3_acceptance.py.
- LIVE021 committed outcomes and attempt diagnostics:
  9ed2e8dd7e5d7fa7ce1b0113931c0014ebe6f59e.
- MESSAGE local sender receipts and incoming history:
  409040524bc444894a4f092035cd5e2bc906b5c2.
- TASKLIST history cursor snapshot:
  8df8ef8623a00e4050258b78863142b206ead1d9.
- Generator candidate baaf7744a94ca126ba0b913b6829f98e2a33b693:
  HOLD unless independent TMCP-01/RUN-02 QA passes and dot explicitly authorizes inclusion.
  Its ancestor 2649bc445252b8af9d34b6eee1dec2ce1ea6a601 alone is not acceptance.
  Do not silently import generated JSON as a substitute for generator source.

The published final SHA, exact parent list, run logs, JUnit, exit receipts,
deterministic schema checks and remote equality are recorded separately in
/workspace/terminal-mcp-artifacts/tmcp07-release-bravo3-20261010/
after the authorized component set is frozen. A file containing its own Git SHA
would change that SHA, so the external signed-off manifest is authoritative
for the precise commit.

## Acceptance evidence and limits

- OAuth: independent 45 PASS against SQLite 3.46.1 and 3.53.4; B1-B4
  fail-closed checks and anchor/HMAC/recovery regressions pass. Source:
  /workspace/terminal-mcp-artifacts/charlie3-oauth-lineage-qa-20261010/.
- Access Events: independent local 35 selected PASS, including revocation
  during callback. Source:
  /workspace/terminal-mcp-artifacts/bravo3-mcp-events-acceptance-20261010/.
  Actual dot signed callback is NOT TESTED and is a mandatory separate canary gate.
- LIVE021 / MESSAGE / TASKLIST: Foxtrot-3 independent source-specific PASS;
  116, 160 and 134+124 tests respectively. Diagnostic three-way combined
  run 216 PASS, exit 0, no manual merge conflict. Source:
  /workspace/tmcp06-qa-foxtrot3-20261010/evidence/.
- Pre-existing test hash-order sensitivity reproduced against pristine
  bde0dec8 (254 PASS, 1 FAIL) and standalone task candidate
  (266 PASS, 1 FAIL); the individual test passes in isolation.
  Treat repeat failures as explicit baseline comparison, not automatic PASS.
- Every integrated Python edit requires current merged-SHA verification.
  Earlier source-specific PASS does not prove the final integrated SHA.
- No real OAuth DB, verifier key, anchor, permissions, FirstByte/BacLOUD
  runtime, or Canonical pointer was modified by this integration.

## Required preservation contracts

1. OAuth clients, refresh grants, pairing audit sequence and revocation remain
   in protected auth.sqlite3, bound with the existing Access verifier key and
   immutable external identity anchor. Never initialize a replacement identity
   from a partially populated or missing protected store.
2. Access Events revalidates the same principal, scope, Access Mesh binding and
   recipient atomically at final subscription persistence. Revalidation errors
   fail closed; delivery is independently reauthorized. No new grant type.
3. Messages remain local to their original server with their existing 24-hour
   TTL. Sender receipts reflect actual recipient state, not an invented
   acknowledgement or interserver relay.
4. Task history pagination respects stream snapshot boundaries. State revision
   and content revision stay distinct; a state-only mutation does not silently
   become a content rewrite.
5. Compact catalogs must reflect real live Python contracts; MessageRecord.deliveries
   requires regeneration on the exact final merged code after the accepted
   generator is merged. Record two identical builds by checksum and inspect
   semantic output, coverage and error contract regressions.

## Gates before handing to canary owner

- Original integration/M3-functional-candidate and all other agents' worktrees untouched.
- Clean local worktree, merge ancestry confirmed for all intended inputs.
- Changed-source lint and format, git diff --check, combined focused regression
  suite with exit/JUnit/log/SHA, deterministic compact schema build with
  no drift and explicit SHA for generator accepted.
- Only ONE full common suite on the frozen final composition, after checking
  with dot that no other agent is already running one. RUNNING is not PASS.
- Release branch push plus exact remote SHA equality; immutable manifest receipt.
- Explicit dot acceptance of final SHA and subsequent separate deployment step.
- Canary instructions: docs/release/TMCP-07-CANARY-RUNBOOK.md.
