# Test workflow and reusable preparation

`python scripts/test.py` separates component feedback, integration/platform checks,
complete candidate acceptance and node probes. Use the project's installed test
environment (`.[test,compact]`) and the intended source checkout. Each run writes an
external `report.json` together with its exact command, source identity and outcomes.
The runner and pytest session hooks keep test configuration isolated.

[AGENTS.md](../AGENTS.md) defines the test authoring workflow: identify the current
responsibility, find its existing owner, and keep one operation and its observable
result in each test. Arrange the state needed for that operation through a fixture
or the owning store. Neighboring services and transports are doubled in component
checks. Own SQLite transactions can remain real when storage is the subject.

## Commands

```sh
# Small current contract and runner feedback.
python scripts/test.py dev

# Reviewed component checks for these owners.
python scripts/test.py component mesh
python scripts/test.py component agents

# Explicit cross-component feedback, including app/auth/commit boundaries.
python scripts/test.py integration mesh
python scripts/test.py integration events

# Linux shell, Unix IPC and kernel-dependent checks.
python scripts/test.py platform linux --plan
python scripts/test.py platform linux

# Native Windows PowerShell, Job Objects, ACL and deployment checks.
python scripts/test.py platform windows --plan
python scripts/test.py platform windows

# Inspect affected selection before running it.
python scripts/test.py dev --changed-from <full-base-sha> --plan
python scripts/test.py dev --changed-from <full-base-sha>
python scripts/test.py integration --changed-from <full-base-sha> --plan

# Complete acceptance on the exact clean candidate.
python scripts/test.py full --expected-sha <full-candidate-sha> --output /tmp/tmcp-acceptance-<unique-id>

# Read-only liveness and release check of an already deployed node.
python scripts/test.py node --url https://node.example --expected-release <version-shortsha>
```

`component` accepts `auth`, `mesh`, `storage`, `contracts`, `commands`, `slots`,
`events`, `tasks`, `agents`, `testing`, `deployment`, and `all`. The source owners,
component allowlist, mixed-function layers and dependency graph live in
`scripts/test_plan.py`. Unreviewed modules remain in integration. Integration mode
includes its requested component checks and their transitive boundaries; platform
checks form a separate explicit batch. `--plan` prints selectors, reasons and
coverage deferred to the other layers. A plan is not a passing behavior check.

Frontend checks use the console's Vitest and build commands. Python `full` targets
all of `tests/` and does not run the console or `qa/windows/`. Native Windows tests
have the explicit `platform windows` target and require a Windows test environment.
The former Linux QA modules duplicated current owners under `tests/` and were
removed during the atomic-test cleanup.

## Selection and acceptance evidence

Affected selection uses declared source owners, direct source imports and transitive
test-helper imports. Changes to a helper can select consumers whose filenames do
not match the owner patterns. Source boundary plans also include consumers of the
owner's declared ports. Prose-only changes under `docs/` and `skills/`, or to
`README.md` and `AGENTS.md`, do not select executable tests. Changes to generated
artifacts remain subject to their executable contract owners.

A shared or unclassified input, dependency lock, deleted test or unresolved import
graph expands to every reviewed component check and exposes integration/platform
coverage separately. Shared storage, authorization and managed-session contracts
also expose all remaining boundaries. The plan's `routes` identify each changed
source path, its owner or explicit unknown status, the fallback reason and boundary
breadth. Unknown source families are declared in `UNKNOWN_SOURCE_FAMILIES`.
`integration --changed-from` runs the affected non-platform boundaries only when
explicitly requested. Static import edges guide feedback; they do not prove every
runtime dependency.

Mixed modules select named functions and preserve pytest parameterization. A stale
mixed-function declaration blocks planning. Paths use POSIX separators on every
host. An empty feedback plan reports `no_tests_selected` and never invokes pytest
without selectors. All component, integration and platform declarations refer to
current tests; helper-only modules remain available to their import consumers.

Full mode forbids selectors, checks the exact SHA and requires a clean checkout.
It removes inherited pytest filters and Python optimization, counts collection and
fixture errors, requires every collected case to pass with none deselected, and
checks for source changes before issuing eligible evidence. `--allow-dirty` is a
diagnostic option and cannot produce eligible full-test evidence. No retries,
xfail conversions, disabled assertions or hidden omissions replace the full run.

The report's `full_test_evidence_eligible` flag certifies those complete test
requirements only. Deployment approval and live acceptance remain separate from
this source test result. The source-only critical gate in
`scripts/stab_release_gate.sh` likewise keeps its exact-SHA, clean-checkout and
pinned SQLite ABI requirements. Its explicit targets refer to the current storage,
command, task, message, Mesh and execution owners; a missing target blocks the
gate. Neither a scoped source gate nor a node liveness check replaces full evidence.

## Current test scope

The cleanup removes duplicate obligations, legacy import/signature guarantees,
retired identity replication and old-schema/rolling-compatibility scenarios.
It also removes source-text assertions, nested test-runner suites, stress loops,
benchmarks and chains that exercised several independent operations. A versioned
filename alone does not establish that its behavior is obsolete: the current Mesh
projection/source modules still own current functionality.

A retained test exercises one current outcome. Existing lower-level owners replace
repeated workflow assertions: task storage owns its durable state, Access Mesh
message storage owns ACK/reply state, and native Access lifecycle owns work-session
fencing. Mesh control, projection and recovery are tested at their own
store boundaries with neighboring nodes injected. Native execution checks retain
the operating-system boundary needed for process cancellation, socket/peer fencing
and bounded output. Failure guards remain when they protect current authorization,
durable data or command ownership.

Specific removals and their remaining owners are recorded in
[test-scope-cleanup.md](test-scope-cleanup.md). Historical counts are audit evidence,
not assertions embedded in the suite. Report current collected counts and full-run
results from the exact source being reviewed. Runtime changes, environment changes
and test removal can affect duration differently; a shorter run alone does not
prove correctness.

## Isolation and report retention

Every run uses temporary SQLite databases, runtime configuration, output, working
directories, sockets, Mesh metadata and Mesh state. Inherited `TERMINAL_MCP_*`
settings are removed. No test-state fallback uses production paths. Reports retain
Python/dependency versions, exact argv, source identity, phase timings, per-test
outcomes and hashes of evidence. Existing report directories are never overwritten.
Fixture setup totals are inclusive; they must not be added as exclusive wall time.

The runner directs `TMPDIR`, `TMP` and `TEMP` into its isolated test state. It removes
that state after pass, failure or timeout, while keeping the report, JUnit XML and
logs separately. Use `--keep-state` for a concrete failure investigation when the
per-test files are needed. The source-only gate also removes its test directories;
`TMCP_GATE_KEEP_TEST_STATE=1` preserves them explicitly for incident reproduction.
Optional `--sample` diagnostics and `--fresh-preparation` remain available to
investigate a named problem; neither changes which tests are selected. The ordinary
workflow does not run a separate benchmark or repeat the suite to estimate cost.

The `node` command uses HTTPX for a bounded `GET /health/live`. It checks liveness
and the public release string, rejects URL credentials and distinguishes an HTTP
failure from a CDN challenge. It cannot establish the full source SHA or exercise
authenticated management routes. Deployment acceptance must verify its artifact
identity and configured MCP/OAuth/management routes through the existing workflow.

## Preparation reuse

`tests/conftest.py` contains explicit preparation allowlists for the remaining
behavior modules. This cleanup only removes stale entries and follows renamed
owners; it does not change the preparation implementation or runtime source.

1. Immutable MCP schema dictionaries are produced by the original generator and
   cached with a bounded key. Every consumer receives a deep copy. Generator
   identity and arguments form the key; unknown or unhashable inputs use the real
   generator. Static model catalogs reuse schema dictionaries without replacing
   runtime validators. Generator/contract tests use their original paths.
2. Selected behavior tests seed a new main SQLite database from a verified empty
   current-version schema. SQLite's backup API seals the template, all application
   tables must be empty, and publication is restricted to absent files inside the
   run's temporary directory. Existing databases are untouched. The complete real
   `initialize()` method still runs, including validation, recovery and transactions.
   Every database has independent contents and a separate inode.

Current initialization, durability, locking and native-process owners retain the
preparation their operation requires. Real ASGI authentication checks use a genuine
secure verifier-key file; Linux-specific permission checks do not establish native
Windows acceptance. Native Windows checks must run on their own platform.
Parallel pytest execution is not enabled: remaining process and global-state
boundaries have not all been proven independent.
