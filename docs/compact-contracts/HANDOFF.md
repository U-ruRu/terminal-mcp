# Handoff — Whiskey-2 offline RKB V4 compact contracts

**Receiver:** dot (current Secondary session X-ray-2). **Worktree:** \`/workspace/terminal-mcp-rkb4-compact-whiskey2-20261010\`. **Published branch:** \`task/TMCP-RKB4-COMPACT-whiskey2-20261010\` in \`U-ruRu/terminal-mcp\`. The exact published commit is the branch tip; confirm with \`git rev-parse task/TMCP-RKB4-COMPACT-whiskey2-20261010\`.

**Source base:** \`integration/M3-functional-candidate\` at \`bde0dec8bd0679c3cbda768104672de57956b6cf\`. Verified algorithm source: \`U-ruRu/research-knowledge-base\` at \`e721d0bea8d47ef042d429f8651783cc5708efbe\`.

**Delivered:** Terminal-owned structural compiler, source-linked runtime-only annotations, deterministically built offline **Access 1 / Executor 10 / Coordinator 8** future \`tools/list\` catalogs, original baselines, strict input/output fallback sidecar, 19/19 rule coverage summary, per-tool/endpoint/full net sizes, pinned tokenizer \`tiktoken==0.12.0 / cl100k_base\`, tests and build instructions. \`docs/compact-contracts/README.md\` explains build/review semantics; \`REPORT.md\` is the acceptance and limits report.

**Key evidence:**
- All 19 original tools/list definitions independently matched actual \`create_app\` effective endpoints.
- Targeted combined tests: **45 passed** (14 new semantic gates + 31 existing schema, output, annotations, runtime tests); final after annotation expansion: **14 new semantic tests passed**. Ruff lint **PASS**. Deterministic offline rebuild \`--check\` **PASS**.
- Total \`tools/list\` changes **57,221 → 54,109 UTF-8 bytes (−3,112; −5.44%)**, **13,763 → 12,789 cl100k_base tokens (−974; −7.08%)**. Access and Executor grow; most net improvement is smaller \`coordinator.task_manage\` schema. Complete detail is in \`generated/metrics.json\`.
- All hard validators, handlers and deployed publication are untouched. OAuth, Events and STAB sections unchanged.

**To reproduce:**

\`\`\`sh
git checkout task/TMCP-RKB4-COMPACT-whiskey2-20261010
python -m pip install -e '.[compact,test]'
python scripts/build_compact_catalogs.py --check
pytest -q tests/test_compact_contracts.py tests/test_role_output_planning.py \
  tests/test_access_mesh_metadata_contract.py tests/test_role_tool_annotations_review.py \
  tests/test_role_wire_runtime.py tests/test_role_mcp_v1.py
\`\`\`

**Remaining gate:** Independent read of \`generated/catalog-{access,executor,coordinator}.json\`, \`generated/coverage.json\` and \`generated/strict-contracts.json\`; confirm correctness of client-facing strict \`inputSchema\`, accept output/error sidecar fallback and selective growth. Only then implement switchable publication in a **separate** task, followed by independent live ChatGPT Access/Exec/Coord A/B testing and proof on real connectors. Do not deploy this branch as part of the handoff.

