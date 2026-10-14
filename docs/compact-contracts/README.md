# Terminal MCP — agent-facing compact contracts

Access (2 tools), Executor (10), and Coordinator (8) publish identical public
input/output schemas: `{"type":"object"}`. Calls still pass the original
flat argument object directly. Names, annotations and tool availability stay
unchanged. This source change is not deployed to any running server.

## Build and drift checks

From the repository root, install `.[compact,test]`, then run:

```bash
python scripts/build_compact_catalogs.py --write
python scripts/build_compact_catalogs.py --check
pytest -q tests/test_agent_object_contract.py tests/test_compact_contracts.py
```

The offline compiler renders `Arguments`, `rules`, `Returns`, `Errors`, and
tool-local `Types used by` from existing FastMCP and strict Pydantic contracts.
Source-anchored runtime rules cover conditionals absent from JSON Schema.
The builder emits ten JSON review artifacts under `generated/`, plus the
packaged runtime `src/terminal_mcp/mcp/agent_catalogs.json`. Its `--check`
mode fails if any artifact, field, model digest, annotation or source changes.

## What each artifact contains

- `catalog-*.json`: agent descriptions and exact object-only public schemas.
- `before-*.json`: the previous public discovery representation for comparison.
- `strict-contracts.json`: unchanged authoritative inputs and outputs for 20 tools.
- `coverage.json`: field, model, condition, action and source-anchor evidence.
- `metrics.json`: UTF-8 and tokenizer costs before and after.
- `provenance.json`: source hashes and compiler identity.

At startup `agent_contracts.py` reads the small packaged catalog and checks
all current input/output model hashes, original descriptions and tool rosters.
The complete compiler and `jsonschema` are not required by the running server.
Mesh and native role catalogs are both bundled.

## Runtime and rollback

Only discovery is generic. Existing strict validation, identity, authorization,
session isolation, domain operations and successful response shapes remain.
A bounded input admission gate rejects pathological JSON before domain work.
Expected validation failures are ordinary MCP results (`isError:false`) with
`structuredContent` containing `ok:false`, `error.code`, `error.reason`,
`error.path`, optional `error.expected` and `outcome:"not_committed"`.
Use `input_validation_failed` for invalid argument contracts and
`input_admission_failed` for payload complexity/size.

Set `TERMINAL_MCP_AGENT_SCHEMA_MODE=legacy` for the previous discovery
representation on the next service start, without data migration. Server
rollout and rollback are controlled separately, starting on Secondary.

Historical Bravo-3 and Echo-3 QA records remain unchanged as evidence of
earlier candidates; the old 19-tool metrics are not current release metrics.
