# Terminal MCP compact contracts (offline generator)

This directory holds reproducible **future** tools/list catalogues for Access (1), Executor (10) and Coordinator (8). The generated candidate has **not** been installed on any live server. The corrected design and B1–B13 evidence are in **QA-REMEDIATION-ECHO3-20261010.md**. Original QA-BRAVO3-20261010.md remains the immutable, independently authored BLOCKING report for the preceding candidate.

## Build and validate

From the repository root in Python 3.11+:

    python -m pip install -e '.[compact,test]'
    python scripts/build_compact_catalogs.py --write
    python scripts/build_compact_catalogs.py --check
    pytest -q tests/test_compact_contracts.py tests/test_compact_bravo3_independent.py tests/test_compact_b1_b13_regressions.py

The build uses actual in-repository FastMCP tool definitions and existing strict Pydantic request/output models, without starting HTTP or changing deployed publication. The generator and annotator are in src/terminal_mcp/mcp/compact_contracts.py and compact_annotations.json. These belong to Terminal MCP. Structural traversal, safe abbreviation, fail-closed exact fallback and type closure derive from RKB V4 compiler (research-knowledge-base SHA e721d0bea8d47ef042d429f8651783cc5708efbe); no RKB domain contracts or TSCG dependency was copied.

The builder is deterministic. --check independently rebuilds the 10 JSON artifacts and exits nonzero when a tracked generated artifact is missing or differs. Generated/provenance.json records source, annotation and generator SHA-256. Runtime annotations have source path and anchor checks. The generator also guards canonical operation roster drift. Tests verify a deliberate stale artifact and a source drift cause failure.

## Transport schema versus strict server contract

- generated/catalog-access.json, catalog-executor.json, catalog-coordinator.json: full **future** result objects, including names, descriptions, output schemas and original tool annotations.
- generated/before-access.json, before-executor.json, before-coordinator.json: exact published-in-process **baseline** tool definitions from the QA-parent checkout including corrected local description strings.
- generated/strict-contracts.json: **unchanged** authoritative Pydantic model JSON Schemas for all 19 inputs and outputs. Its file SHA-256 matches the prior Bravo-3 QA source artifact. This is a **review sidecar**, not the transport inputSchema to be sent to a prevalidating client.
- generated/coverage.json: every tool's model, field roster, strict input/output digest, nullable/conditional rules, source-linked runtime annotations, action-specific allowed/required/forbidden fields and exact fallback.
- generated/metrics.json: entire before/after tools/list bytes, characters, pinned tiktoken 0.12.0 / cl100k_base tokens, plus per-tool/component contributions and shares.
- generated/provenance.json: generator, reference RKB SHA and digest of relevant source.
- qa-echo3-wire-metrics.json: **complete** HTTP and result JSON-RPC error sizes from the 23-case isolated MCP replay.

The **public** inputSchema is an object transport envelope: known argument names each have empty schema, unknown fields are allowed, no client-side value/required/action constraints. Strict server validation remains intact and returns compact public errors. The **description** now explicitly enumerates fields/types/default/nulls/conditions and per-action whitelists/non-null required values, and retains exact fallback where abbreviated equivalence is not proven. Action-model descriptions are generated from canonical source models rather than manually maintained lists.

The public outputSchema remains the already accepted flat projection. The exact nested result/error models remain in the sidecar; future independent review and live tests must cover the difference.

## Verification and limitations

The prior independent Bravo-3 source checks and MCP-wire replay are preserved; the test updates only replace obsolete assumptions about client-side strict schema validation. The isolated test installs the proposed descriptions/input schemas **in memory** on an unexposed create_app instance and verifies all 19 tools/list records match the generated catalogue before making HTTP calls.

Current published/live servers are unchanged. The remaining live gate is a fresh ChatGPT connector A/B test: malformed-but-object arguments must reach the server, responses must stay compact **including pre-tool failure presentation**, tool selection must remain correct, and all authorized successful operations still need verification. The MCP object transport format cannot prevent a client from enforcing additional private rules, nor can offline tests measure live ChatGPT token billing. Future accepted changes in unrelated output schemas require regenerating the catalogue on that later integrated SHA.

OAuth, Events, active release branches and deployments are outside this task.
