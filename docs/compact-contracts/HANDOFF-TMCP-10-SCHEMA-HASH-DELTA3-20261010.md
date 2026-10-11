# TMCP-10 / Delta-3 — schema/hash contract reconciliation

**Scope:** Secondary / Solvenger legacy, own worktree and
`task/TMCP-SCHEMAHASH-DELTA3-20261010`, based on immutable published
`9c23bd6762fc1b06af6fce963befe6a18835c546` in
`U-ruRu/terminal-mcp`. The preceding reviewed comparator is
`bde0dec8bd0679c3cbda768104672de57956b6cf`.

**Decision:** Update only three reviewed JSON snapshots. The discrepancy
is an *already integrated, additive, optional output field* on the real
wire model, **not** an order-dependent hash and **not** an input/role
permission or generator regression. Preserve the integrated receipts; do
not revert the output to suppress the failures.

## Exact failure provenance and approval

Source: `/workspace/terminal-mcp-artifacts/tmcp08-run01-bravo3-20261010/HANDOFF-SCHEMA-HASH-3.md`,
`/workspace/terminal-mcp-artifacts/tmcp08-run01-bravo3-20261010/JUNIT_TRIAGE.json`,
and exact `tmcp07-release-bravo3-20261010/FULL_SUITE.{junit.xml,log,exit}`.

* `tests/test_access_mesh_metadata_contract.py::test_three_effective_role_contracts_match_reviewed_manifest`
* `tests/test_application_boundary.py::test_architecture_a_preserves_exact_published_mcp_contracts`
* `tests/test_task_projection_core.py::test_all_seven_legacy_discovery_contracts_preserve_baseline`

Both reviewed legacy JSONs and the access-mesh manifest in `9c23bd6`
match the `bde0dec8` reviewed files; old runtime schemas reproduce
those frozen manifests exactly. The integrated message receipt change is
traceable to `9957779725646efd80474e8c58426c000130c6bc` (merged at
`9b1d479`), and was deliberately included in the current release
candidate. Zulu-2 expressly approved **only** these three additive
snapshot changes on 2026-10-10 at 23:53:12 UTC, message
`secondary:msg:DjMkkBzUOHixExN4`. Dot later refreshed its legacy
session (`ws_DpWVOg1Sd6b4RvMx-VLT2lXL`, until 00:16:35 UTC), retaining
that limited approval.

## Field-level audit — role manifest

Comparator:
`src/terminal_mcp/mcp/access_mesh_schema_baselines_v2.json` versus
`mesh_schema_manifest` from three FastMCP role servers under
synthetic, isolated `Settings`. Old-vs-reviewed: **zero differences**.
New-vs-reviewed: **exactly six** `runtime_output_digest` changes:

| Role / tool | Old runtime output SHA-256 | Updated runtime output SHA-256 |
| --- | --- | --- |
| coordinator / message | `2af296b4bf62a50983a6595018792c4d7d4234b294ea5bc76e387fd92c30f1ee` | `51d14303a76bde83c51c6af4aa161751ef5187612e42012310627fd8d7992ff7` |
| executor / message | same old message digest | same updated message digest |
| executor / command_run | `14c98739ced16742aa2b143b0e50cb06f6bc26daad6f41718f9a0e82f4fbeb20` | `29d5885337c5fd75bd6731ae68e97c387c5ff73523fd5e6e129a72894b8da3ff` |
| executor / command_cancel | same old command digest | same updated command digest |
| executor / command_recovery | same old command digest | same updated command digest |
| executor / command_read | `8148e53ec2ab68efcbeeea98520136bef88cc291659ce6dee3c421a3544d280a` | `74f3fc4e0fe00ad1f18539fe8090cdea8731ea3e7c9b8983951f88e1a6812733` |

The schema changes are identical in each affected runtime output
closure:

* Addition of `$defs.MessageRecipientReceipt` with
  `additionalProperties:false` and eight optional properties:
  `public_name`, `state`, `first_seen_at`, `last_seen_at`,
  `seen_count`, `read_at`, `replied_at`, `reply_message_hash`.
* Addition of `$defs.MessageRecord.properties.deliveries`:
  nullable optional array of `MessageRecipientReceipt`, default
  `null`. It is **absent from `required`**.
* No existing property, closed-field rule, discriminator, identity
  binding, field limit or `required` list changed.
* All 19 published role-tool input-schema digests, planning
  input/output digests and byte sizes, annotation flags, role endpoints
  and tool rosters are **unchanged**. Compact descriptions and
  action-specific strict schemas remain those from the already
  accepted B1–B13 generator, without edits here.

The command output schemas include the same `MessageRecord` through
their message/coordination payload types; that is why four executor
command tools drifted in the strict **runtime output** digest even
though command arguments and planning payloads did not change.

## Field-level audit — seven legacy tools

The two fixtures
`tests/fixtures/architecture_a_mcp_contract.json` and
`tests/fixtures/legacy_mcp_projection_contract_hashes.json`
encode the same canonical `tools/list` `sha256(json.dumps(...,
sort_keys=True, separators=(',', ':'), ensure_ascii=False))` contract.

In **each** full legacy `cmd` and `message` object, the only
differences versus `bde0dec8` are the two output schema paths
`outputSchema.$defs.MessageRecipientReceipt` and
`outputSchema.$defs.MessageRecord.properties.deliveries` above.
`inputSchema`, descriptions, annotations, names and all other
output definitions are byte-equivalent under canonical serialization.
The five remaining legacy tool objects are unchanged.

| Tool | Old digest | Approved digest |
| --- | --- | --- |
| cmd | `d34b16e886f84b75ec1c19d8b35560c74b2072a7c090d1d1c8c06af21e71a161` | `fd837b0ffb81258baa0578f26a6a0279e2538210987ba12a3d44d12443a8f7fe` |
| message | `94dcfc81ff16e28a80736eebad9c20369a80a6c3210e5f4b9de730d057cfc9e5` | `ff046813d5510fc19bb25e6c479fe4e9fc6fae12560348ffde9ddb1ca4105c2c` |

The earlier historic **session-only** hash-order issue is unrelated:
the session digest here remains `a976ffe5a7f5b2ef9868c2bf146b0cccb8dd5cf7f11e3cc13d8d194b8d3d4801`.
No old session baseline was substituted.

The optional receipt output carries truthful per-recipient sender
history, while recipient-facing incoming messages omit
`deliveries`. This behavior is covered by
`tests/test_local_message_sender_receipts.py`, with no change to
`MessageRecord.delivered_to`, local messages, identity bindings or
compact public validation errors.

## Strict model, generator, tests and reproducibility

* `terminal_mcp.mcp.compact_contracts.build()`'s strict output is
  byte-for-byte equal to
  `docs/compact-contracts/generated/strict-contracts.json`, SHA-256
  `58deeca2d525422fe7584b747cb355eb9df05e44b6434cfc6ec1a58cb13ded52`.
  The generated strict contract already includes the optional
  `deliveries` field before any snapshot update.
* `scripts/build_compact_catalogs.py --check`: **PASS 10/10** at
  `PYTHONHASHSEED=0`, `1`, `8675309`. Two consecutive independent
  `--write` runs at seeds 0 and 8675309 followed by another
  `--check` preserve all ten JSONs. The checksum of each sorted
  `sha256sum *.json` list is always
  `2c6526aca94682250cade2443e0b428da14d98e13d9cebd35b8c3efacba7f0ca`.
* Original three test node IDs: **3 passed**, one pre-existing
  `StarletteDeprecationWarning` (4.99 s).
* Affected set: `test_local_message_sender_receipts.py`,
  `test_mcp_output_contracts.py`, `test_compact_contracts.py`,
  `test_compact_b1_b13_regressions.py`,
  `test_compact_bravo3_independent.py`,
  `test_compact_tmcp05_completion.py`: **156 passed**, one same
  warning (19.08 s).
* Ruff `check` on touched contract modules and pertinent tests:
  **PASS**. `git diff --check`: **PASS**.
* Two frozen legacy hash fixtures and one reviewed mesh manifest are
  the only source JSON changes; `output_contracts.py`,
  `compact_contracts.py`, `roles.py`, tests, OAuth fixtures and
  global `conftest` were **not modified**.
* Raw proof:
  `/workspace/terminal-mcp-artifacts/tmcp10-schemahash-delta3-20261010/`:
  `FIELD-LEVEL-DIFF.md`, old/new `mesh.json`, `role_full.json`,
  `legacy_full.json`, SHA lists, `TARGETED-THREE.log`,
  `AFFECTED.log`, `GEN-CHECK-SEED-*.log`.

## Handoff / release limits

This fixes the three isolated reviewed schema/hash nodes at the
specified candidate base; it does **not** certify an entire release.
TMCP-09 owns OAuth migration fixtures and the unrelated seven other
prior JUnit failures. The full suite, deployment, live client A/B and
canaries were intentionally **not run**. The coordinator should
review this narrow commit, obtain independent QA as required and
only then schedule the separate new full-suite release gate.
