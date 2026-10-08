# Terminal MCP QA handoff — Oscar-20 — 2026-10-09

Scope: separate QA only, 23-minute session, Secondary source and tests; test deployment targets FirstByte then BacLOUD, never Secondary/Main/Tokyo.

## Frozen source and QA test evidence
- Baseline canonical `2cd9770ebedea05a424f178d3b5bd0cf0388001f` (0.14.3): **1869 passed / 15 failed** in 365.68s; `/tmp/qa_sierra_release_0143/pytest.log`. Ruff/privacy pass. Four custom HTTP/MCP Access tests pass.
- QA-only branch `qa/TMCP-0143-contract-regression-oscar21`: commits `1969f34` and `7636319`. Updated obsolete tests for Access privacy, ACK/ALERT full gate, three notify presentations, CAS revision unchanged on claim expiry, output schema budgets (always <=2048), and Task API free-state semantics. Runtime code unchanged. Full at `1969f34`: **1874 PASS / 10 FAIL** (one extra unrelated finalization retry flaky; same test three standalone reruns pass), log `/tmp/oscar_0143_qa_full_1969f34.log`.
- New A regression QA-only assertion: cooperative task owner releases claim, then repeats their own release while another co-owner still live. Baseline wrongly returns `not_owner`; acceptance requires successful no-op, leaving other owner and content CAS untouched. Repro in `tests/test_v010_workflow_model.py::test_claim_intent_owner_participants_owner_handoff_and_owner_only_mutations`, log `/tmp/oscar_0143_legacy_state_refresh2.log`. Updated QA branch at `7636319` contains it.
- Verified two genuine regressions introduced before 0.14.3: explicit operator HTTP `idempotency_key` ignored (despite required public HTTP field), and lost `review_feedback` events when a linked review changes state. Baseline `5817c42` review feedback passes and `98ea439` fails. Base `a6de118` old explicit operator HTTP retry tests 2 PASS; candidate `2cd9770` both FAIL. Recorded detailed comments in Task A and Task B and messaged developer.

## Independently verified fixed candidate
- Developer's source-only candidate committed `1d02022` (0.14.3). Read-only detached `/workspace/terminal-mcp-qa-final-1d02022`, copies of QA-only test files: cross-feature **25 passed / 0 failed**, Ruff passed; `/tmp/oscar_qa_final_1d02022_focus.log`. The exact four critical original regressions on prior snapshot `0366a31` passed 4/4; `/tmp/oscar_qa_abc_0366a31_focus.log`.
- Frozen, CLEAN `1d02022` (no QA test modification) at `/workspace/terminal-mcp-qa-frozen-1d02022`: `TMCP_QA_REQUIRE_CLEAN=1 TMCP_QA_EXPECT_VERSION=0.14.3` gate succeeded, Ruff and privacy passed, **16 focused tests passed**, `/tmp/oscar_qa_0143_release_gate_1d02022/report.md`.
- QA-only `qa/TMCP-0143-contract-regression-oscar21` was pushed to origin at `7636319`. Canonical remained `2cd9770` at last check. The developer's final full suite and linear merge must be confirmed before release.

## Deploy gate / access issue
- Last inspected live FirstByte and BacLOUD: health healthy, installed **0.14.2**, SHA `5817c42` (prior release). No production deploy occurred during QA session.
- Attempted authorized legacy Access session for safe deployment preflight on both live targets, but FirstByte returned `access_denied` and BacLOUD `authority_unavailable` / unknown. Did not retry writes or bypass. BOTH temporary legacy Access sessions were explicitly ended.
- Do not deploy until final source SHA is merged canonically and frozen clean; full pytest green, Ruff and privacy pass; version `0.14.3` matches target; backups/restoration and public compatibility verified; authorized read-only preflight works. Release FirstByte first, then BacLOUD, same SHA; live regression include task state, cooperative release, history/review fanout, explicit HTTP idempotency, command run/cancel, ACK/ALERT and directed messages. Increment version after each completed deployed task.

## QA toolchain
- `/workspace/terminal-mcp-qa-oscar/scripts/qa_release_gate.sh` supports `TMCP_QA_EXPECT_VERSION` and `TMCP_QA_REQUIRE_CLEAN`; produces metadata/ruff/privacy/pytest logs and refuses red gate.
- `/workspace/terminal-mcp-qa-oscar/scripts/qa_compare_pytest_failures.py` diffs full pytest failure lists.
- Historical QA notes in `/workspace/terminal-mcp-qa-oscar/QA_ACCEPTANCE_20261009.md`.
