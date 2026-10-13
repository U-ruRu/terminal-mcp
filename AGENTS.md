# Terminal MCP agent workflow

Follow the current contract and the user's task scope. These instructions govern
test work; detailed runner modes and evidence requirements are in
[docs/testing.md](docs/testing.md).

## Reuse before adding tests

1. Name the behavior and the component that owns it. Inspect its current callers,
   models and contract before treating a versioned/legacy scenario as obsolete.
2. Search existing checks first: `rg -n '<behavior or operation>' tests qa`.
   Read the closest test and its fixtures. Reuse its existing case, update its
   expectation when the authorized contract changes, or extend its parameter table
   when the same invariant needs another meaningful boundary/value.
3. Add a test only after naming the uncovered case and why the closest existing
   test cannot cover it clearly. A useful regression with a concrete trigger or
   failure boundary is welcome. Avoid another test of the same invariant merely
   because a new task/file/agent produced it. Do not manufacture a test count goal.
4. Each component test checks one current responsibility. Replace neighboring
   runtime/transport/store ports with doubles. A storage test may use its own real
   temporary SQLite database and transactions. Keep actual app/auth/store wiring,
   multi-node scenarios, shell/IPC/kernel behavior in their explicit acceptance
   layer. Separate compound obligations so failures identify the responsible one.
5. Preserve current authorization, managed-slot and durable-data guarantees.
   Remove a prior compatibility guarantee or retired scenario only with a short
   current-contract explanation and a mapping to any remaining obligation.
   Record meaningful removals in [docs/test-scope-cleanup.md](docs/test-scope-cleanup.md).

## Choose the smallest relevant run

Inspect a plan before a broad run. Prefer the direct component or existing test
function you changed. The existing runner keeps reports/state isolated; use its
installed Linux environment (`.[test,compact]`). Examples:

```sh
# Isolated Events storage and projection logic.
python scripts/test.py component events --plan
python scripts/test.py component events

# Its actual app/auth/binding/commit/restart boundaries, when those are relevant.
python scripts/test.py integration events --plan
python scripts/test.py integration events

# Declared owners and helper consumers for an existing change.
python scripts/test.py dev --changed-from <base-sha> --plan
python scripts/test.py integration --changed-from <base-sha> --plan

# One existing function, in a disposable configured test environment.
python -m pytest -q tests/test_task_projection_core.py::test_receipt_projects_claim_owner
```

Use `component events`, `component tasks`, `component agents`, or another existing
owner for direct feedback. Check the plan's deferred integration/platform coverage.
Expand because of a named boundary risk: changed auth/lease/commit ordering,
persistence/recovery, public adapter wiring, real process behavior, or a failed
relevant check. Use `platform linux --plan` for native requirements. Full acceptance
belongs to an exact release candidate or an explicit request; do not start it for
every source/document edit. Do not benchmark a heavy suite just to estimate cost.
Unclassified changes may broaden component feedback; inspect that selection and
explain the relevant scope rather than automatically escalating to full.

Register a separated atomic module in the existing `scripts/test_plan.py` allowlist
and its owner patterns; known mixed functions belong in its existing layer table.
Do not create a new runner, generic test framework or duplicate workflow document.
New selection rules need focused regressions for the concrete selection failure.

## Report what was checked

Give the intended behavior, changed files, exact commands, cases/outcomes and useful
duration evidence. Identify **reused**, **changed**, and **new** tests; for each new
case state the gap, and for each removal state retirement or current replacement.
List deferred native/integration/release checks and actual blockers. A selection
plan or `no_tests_selected` is not a passing behavior check. A scoped report never
substitutes for eligible full evidence, operator approval or a live acceptance.
