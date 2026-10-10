# Bravo-3 QA checkpoint

Session `ws_My6uQouhq0vZRtMGlJ530mi7` (legacy Bravo-3), expires 2026-10-10T21:37:47Z. Candidate `d1caea24e2d8c2dff650568fac616bcee0efb99b`. Worktree `/workspace/terminal-mcp-compact-qa-bravo3-20261010`.

**Checked:** all 19/19 role/tool entries (Access 1 / Executor 10 / Coordinator 8), source input/output models, runtime handlers and descriptions; all 9 task_manage actions; null, types, enums, bounds, nested/union, required, defaults, additionalProperties, runtime conditions, update/replace. Isolated MCP invalid + valid-boundary requests 19+19 and stress 4; **43 pytest PASS**. Rerun metrics and --check baseline PASS; intentional artifact/source drift detected, restored baseline PASS. Independent size: **57221 → 54109 bytes; 13763 → 12789 cl100k_base tokens**. Error measurements in `qa-bravo3-wire-metrics.json`.

**Remaining:** re-review corrected candidate SHA (none available in this QA session), expose corrected future catalogs on test connector only after fixing discrepancies, then live ChatGPT client A/B proving bounded pre-tool errors; integration/deployment/authenticated success not tested.

**Verdict:** BLOCKING; findings B1–B13 in `QA-BRAVO3-20261010.md`. Source candidate, Canonical and live fleet untouched.
