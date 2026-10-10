# Terminal MCP RKB V4 compact compiler — offline acceptance report

**Status:** offline candidate ready for independent schema review; **not deployed**. This review covers the Access-Mesh-effective MCP publication on parent \`bde0dec8bd0679c3cbda768104672de57956b6cf\`, with 19/19 tools (Access 1, Executor 10, Coordinator 8). Structural compiler algorithms derive from independently verified RKB \`e721d0bea8d47ef042d429f8651783cc5708efbe\`; all emitted data/types are Terminal's.

**Source:** \`src/terminal_mcp/mcp/compact_contracts.py\`, \`src/terminal_mcp/mcp/compact_annotations.json\`; **entrypoint:** \`scripts/build_compact_catalogs.py --write|--check\`. Offline tools/list \`{"tools":[...]}\` exact proposed output: \`generated/catalog-{access,executor,coordinator}.json\`. Comparison baseline: \`generated/before-*.json\`. Strict authoritative reference: \`generated/strict-contracts.json\`. Rule-level coverage: \`generated/coverage.json\`. SHA/determinism: \`generated/provenance.json\`.

## Net full-catalogue size

Measured by canonical UTF-8 \`json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False)\`; tokens via **tiktoken 0.12.0 / cl100k_base**, not an estimate. Both variants include every \`Tool.name\`, \`description\`, \`inputSchema\`, \`outputSchema\` and \`annotations\`; metrics include per-tool schema and description component shares, available in \`generated/metrics.json\`.

| Endpoint | Full UTF-8 bytes before | Full UTF-8 bytes after | Difference | Tokens before | Tokens after | Token difference |
|---|---:|---:|---:|---:|---:|---:|
| Access | 1,120 | 1,819 | +699 | 269 | 429 | +160 |
| Executor | 16,009 | 26,500 | +10,491 | 3,675 | 6,258 | +2,583 |
| Coordinator | 40,092 | 25,790 | −14,302 | 9,819 | 6,102 | −3,717 |
| **Three catalogues total** | **57,221** | **54,109** | **−3,112 (−5.44%)** | **13,763** | **12,789** | **−974 (−7.08%)** |

Entire catalogues become **smaller overall** but the result is uneven. Almost all net saving comes from \`coordinator.task_manage\`, **−20,865 bytes**, replacing a 32 KiB planning projection with an exact, much more compact strict Pydantic input schema plus explicit action-specific rules. Other tools are larger: Exec +10,491 bytes because required/optional field visibility, server constraints and runtime rules are now explicit. Access +699 bytes. This is a modest net saving **for this specific serialized corpus and tokenizer**, not a general claim that every tool/connector is smaller.

All 19 tools have their own exact output fallback in \`strict-contracts.json\`. The flat public \`outputSchema\` is unchanged on purpose. Future integration must confirm the target ChatGPT connector correctly uses the strict input schema; that is out of scope for offline review. The real token exposure and behavioral success after deploy still need A/B testing.

## Verification and semantic rule inventory

- Original tools/list snapshots independently compared against **real app assembly via \`create_app\`** for Access, Executor and Coordinator: **exact 19/19 matches**.
- All 19 names, operations, strict input model identities, field lists, required fields, constraints, Pydantic schema digests, strict output model digests, published annotations, endpoint roster and output schema hashes checked. All JSON Schemas pass Draft 2020-12 checks. All local \`$ref\` resolve.
- Tested task-list valid and invalid filters, cursor, bounds, wrong types and unknown fields on both Exec and Coordination. Tested Access four-digit attachment and forbidden start operation.
- Tested message \`send\`/\`ack\`/\`reply\`/\`read\`/\`history\`/\`recipients\` conditional requirements, forbidden field combinations and runtime-only Pydantic \`model_validator\`. Verified source-backed message scope annotation. Tested Exec comment vs checkpoint required values.
- Tested Coordinator create/state/archive/relate input action conditions. Explicitly differential-tested the upstream JSON Schema and **downstream canonical action-specific request models**, because the top-level boundary does not itself enforce every \`json_schema_extra\` clause. Runtime-dependent state transitions are noted as source-linked annotations; handler behavior is unchanged.
- Tested strict fallback on unknown schema assertions, union-with-siblings, missing ref; schema-context stripping that preserves fields called \`description\`, \`title\` or \`example\`; only sound \`oneOf\` discriminator exclusivity proofs; stale annotations and artifact modifications.
- **45 tests passed** for targeted semantic plus existing role schema/output/metadata/wire regression suite (pre-final annotation addition); **14 semantic tests** rerun after final annotation update. No server or OAuth change. Lint via Ruff passes. \`--check\` confirms reproducibility.

## Limitations, future gate

The generated \`description\` is intentionally **not a standalone copy of the entire strict JSON Schema**: it includes existing manual description, condensed navigation rules, source-linked runtime-only conditions and summarized response variants. The full lossless machine-readable \`inputSchema\` is adjacent in each tool; nested response and error variants have exact sidecar fallback. This avoids parallel unsourced manual descriptions while requiring clients and reviewers to read schema as well as prose. A deployment owner must explicitly accept this publication contract before routing real \`tools/list\` to the generated variant.

The pinned tokenizer is \`cl100k_base\` and may differ from actual product model accounting. These are serialization measurements, not measured usage costs.

**Out of scope:** OAuth isolation, Events, server validation, request handler or output changes, deployment, live end-to-end ChatGPT testing and TSCG.

