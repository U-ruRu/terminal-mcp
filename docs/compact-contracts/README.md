# Terminal MCP compact contracts — offline release candidate

This directory contains a **reproducible, offline-only projection** of the effective Access Mesh MCP tool catalogues. It does not modify the running FastMCP server, canonical request validators, handlers, authorization, output projection or public errors. Deploy and connector A/B testing are **separate** acceptance stages.

## Provenance and scope

- Terminal MCP source: \`integration/M3-functional-candidate\` at \`bde0dec8bd0679c3cbda768104672de57956b6cf\` (the reproducible parent of this candidate).
- Algorithm source: \`U-ruRu/research-knowledge-base\` at \`e721d0bea8d47ef042d429f8651783cc5708efbe\` (RKB V4 structural compiler). The implementation is **native Python owned by Terminal MCP**, not copied RKB domain contracts, generated descriptions, or TSCG experiments.
- **19** effective tools: Access **1**, Executor **10**, Coordinator **8**, using Access-Mesh-specific \`AttachInput\`, \`MeshMessageInput\`, \`MeshCommandReadInput\` and \`MeshTaskCommentInput\` rather than generic standalone inputs.
- Strict input contract comes from current Pydantic \`model_json_schema()\`, and exact strict outputs from the server-side output models. The original \`ToolAnnotations\` and flat \`outputSchema\` are preserved. The future offline \`inputSchema\` is the metadata-stripped strict projection, augmented with unchanged \`x-terminal-mcp-action-matrix\`.

## Build / verify

From the repository root, in a Python >=3.11 environment with the application installed:

\`\`\`bash
python -m pip install -e '.[compact,test]'
python scripts/build_compact_catalogs.py --write
python scripts/build_compact_catalogs.py --check
pytest -q tests/test_compact_contracts.py
\`\`\`

Both commands build **actual FastMCP tool definitions** without starting the HTTP service. \`--write\` regenerates all ten JSON artifacts; \`--check\` compares every resulting byte with the committed artifact and exits nonzero on an unexpected or missing result. \`provenance.json\` records SHA-256 of relevant source files, the generator and runtime annotations, plus exact roster. The compiler also resolves every tool-local \`$ref\`; tests explicitly check dangling refs and any stale source anchor.

All paths are repository-relative: \`src/terminal_mcp/mcp/compact_contracts.py\` is the structural compiler, \`compact_annotations.json\` contains runtime-only rules linked to verified source locations, and \`scripts/build_compact_catalogs.py\` is the build/check entrypoint.

## Outputs

\`docs/compact-contracts/generated/\` contains:

| File | Purpose |
| --- | --- |
| \`before-access.json\`, \`before-executor.json\`, \`before-coordinator.json\` | Exact effective pre-change \`{"tools":[...]}\` catalogue snapshots |
| \`catalog-access.json\`, \`catalog-executor.json\`, \`catalog-coordinator.json\` | **Full proposed future** \`{"tools":[...]}\` catalogues for offline review |
| \`strict-contracts.json\` | Unmodified authoritative **input and output** schemas per \`role.tool\`; precise response/error fallback |
| \`coverage.json\` | Source model, digests, field lists, required fields, conditions, referenced types, exact fallback paths and annotated runtime rules for all 19 tools |
| \`metrics.json\` | Full before/after tools/list sizes and per-tool/per-connector sizes: UTF-8 bytes, Unicode characters, \`tiktoken==0.12.0 / cl100k_base\` tokens; description and schema component ratios |
| \`provenance.json\` | Source and generator SHA-256 and expected endpoint tool rosters |

Size measurements serialize the **entire** \`{"tools":[...]}\` object with \`json.dumps(sort_keys=True,separators=(',', ':'),ensure_ascii=False)\`. They exclude the JSON-RPC transport envelope and OAuth headers on both sides. The three-connector total is the **sum** of their separate \`tools/list\` responses. Token counts use a pinned \`cl100k_base\` tokenizer, not an estimate or a claim about the live ChatGPT tokenizer.

## Semantic preservation strategy

1. No source JSON Schema object is interpreted through string replacements. The compiler traverses nested schema objects with **schema-vs-data context**: metadata \`description/title/examples/$comment\` is stripped **only as a schema keyword**, retaining user fields with those names.
2. Local \`$defs\` remain inside the published input schema and are visited as a per-tool closure. Required fields, nullable branches, nested restrictions, allowed operations, defaults, patterns and bounds are in the machine-readable input schema. The text gives a compact navigation aid and source-linked runtime rules.
3. Conditional \`if/then/else\`, \`allOf\`, \`anyOf\`, \`oneOf\`, \`dependentRequired\`, and \`dependentSchemas\` are converted only under established syntactic equivalence. The disjointness proof does not treat \`integer\` and \`number\` or optional discriminator keys as disjoint. Unrecognized assertion keywords and composite-with-siblings become \`SCHEMA_FALLBACK\` with exact serialized JSON, or the compiler raises on unresolved references.
4. Runtime-only conditions are explicitly enumerated in \`compact_annotations.json\`, with verified source path and text anchor: message operation constraints, local messaging scope, coordinator task-create idempotency identity, task-manage action validation and task list projection. They survive without adding Pydantic validators or changing handler behavior.
5. Existing descriptions remain as a **verbatim prefix** of each generated description. No prior manually-authored constraint is silently dropped. Redundant field type/type-closure prose is excluded when the **adjacent exact \`inputSchema\`** provides it; short tools can have inline argument hints.
6. Current flat \`outputSchema\` is unchanged, preserving public MCP compatibility and established schema-size budgets. Exact nested response variants and public error shapes/codes live in \`strict-contracts.json\` and are documented as **exact fallback** in \`coverage.json\`. These sidecar schemas are intended for independent offline review and must be checked before replacing any flat output contract.

## Tests and known limits

\`tests/test_compact_contracts.py\` cross-checks the generated output with actual \`create_app\` catalogues, all 19 strict model/schema identities, valid/invalid task-list requests, operation-conditional message requirements, coordinator task mutations through **both** published JSON Schema and the authoritative downstream \`_TASK_MANAGE_MODELS\`, runtime-only null / forbidden-field cases, Access attachment, comment/checkpoint, missing references, type-disjointness, unknown-keyword preservation, annotation anchors and stale artifact detection. Existing role/metadata/output regressions are run separately.

**Not yet integrated in the running server:** generated files are review artifacts only. Live \`tools/list\` remains unchanged; fresh ChatGPT connector usability, latency, token accounting and A/B calling behavior require a future accepted integration plus a separate deployment. A consumer that discards the published input schema and reads only description prose will not see the exact machine-readable constraints. Similarly, the flat published output schema does not inline the strict nested output/error fallback; offline review must inspect \`strict-contracts.json\`.

Some conditional requirements are marked in Pydantic schema extras yet enforced downstream by canonical action models rather than the permissive top-level model. Tests deliberately exercise both validation layers. The project avoids promoting heuristic string descriptions to validation authority.

TSCG is not a dependency. OAuth storage/identity and Events code have not been touched.

