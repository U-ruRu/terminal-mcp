# Test workflow and reusable preparation

`python scripts/test.py` separates component feedback, integration/platform checks,
complete candidate acceptance and node probes. Run it using the project's
installed test environment (`.[test,compact]`) and a checkout of the intended source.
It prints an external `report.json` path; results never disappear into terminal output.

The repository's [AGENTS.md](../AGENTS.md) defines the lightweight agent workflow:
find and reuse existing behavior checks before adding a named missing regression,
select the smallest relevant layer and report reused/changed/new coverage.

## Commands

```sh
# Development feedback: public/compact artifact contracts, read/window/version and runner safeguards.
python scripts/test.py dev

# Reviewed component checks for this owner only; no global dev or dependency expansion.
python scripts/test.py component mesh
python scripts/test.py component capture

# Explicit cross-component feedback. Capture includes the app/auth/commit boundaries.
python scripts/test.py integration mesh
python scripts/test.py integration capture

# Explicit Linux shell, Unix IPC and kernel-dependent checks.
python scripts/test.py platform linux --plan
python scripts/test.py platform linux

# Inspect affected selection, including committed, staged, unstaged and untracked changes.
python scripts/test.py dev --changed-from <full-base-sha> --plan
python scripts/test.py dev --changed-from <full-base-sha>
python scripts/test.py integration --changed-from <full-base-sha> --plan

# Candidate acceptance: one complete run on an exact, clean source checkout.
python scripts/test.py full --expected-sha <full-candidate-sha> --output /tmp/tmcp-acceptance-<unique-id>

# Read-only check of an already deployed node. Use its exact expected public release string.
python scripts/test.py node --url https://node.example --expected-release <version-shortsha>
```

`component` accepts `auth`, `mesh`, `storage`, `contracts`, `commands`, `slots`,
`events`, `capture`, `tasks`, `agents`, `testing`, `deployment`, and `all`. The source
owners, reviewed component allowlist, mixed-function tiers and dependency graph are
in `scripts/test_plan.py`. Unreviewed modules remain in integration until their
responsibilities are separated. SQLite transaction tests are component checks when
storage itself is the subject; a POSIX-looking filename is not a platform requirement.
Integration mode includes requested component checks and their transitive boundaries;
platform checks remain a separate explicit batch. `--plan` prints exact selectors,
reasons and deferred integration/platform coverage. None of these modes is a release gate.

Affected selection uses declared source owners, direct test imports of source modules
and transitive test-helper imports. An import can select a consumer whose filename
does not match the owner's patterns (for example WorkWindowStore or Android scope).
Source boundary plans also include consumers of the owner's declared ports.
Documentation selects current documentation/artifact contracts. A shared/unclassified
input, dependency lock, deleted test or unresolved import graph expands to every
reviewed component check, with integration/platform coverage printed separately.
It does not silently start a full release run. `integration --changed-from` explicitly
selects the affected integration batch. This is staged feedback, not a proof that
unselected runtime paths are unaffected. Frontend checks still use the console's
Vitest/build commands; Python `full` does not run them.
The three dedicated modules under `qa/` are also separate explicit pytest targets
(50 collected cases in this audit); the default `testpaths` and Python `full` target
only `tests/`. The layer inventory above describes that existing runner's corpus.

The plan's `routes` records each changed source path, owner or explicit unknown
status, fallback reason and boundary breadth. Shared auth/storage and managed-session
policy contracts keep direct component feedback but expose **all** remaining
integration/platform checks; their dynamic/injected consumers are not narrowed by
filename guesses. `integration --changed-from` selects that broad non-platform batch
only when explicitly requested. Native checks stay deferred. Direct source import
edges are static evidence, not a claim of complete runtime import transitivity.

The reviewed source snapshot has 188 tracked files (183 Python modules and five
assets): 115 have an owner route and 73 use the explicit unknown route. The unreviewed
families are 20 HTTP adapters, 21 shared application modules, 14 shared core modules,
two adapters and 16 package/composition modules. Examples are `http/persistent.py`,
`application/base.py`, `core/orchestration.py`, `adapters/mcp_identity.py` and `app.py`.
Their full family fallbacks are in `UNKNOWN_SOURCE_FAMILIES`; a new/unmapped source
also selects every reviewed component check and exposes all boundaries. No module
silently disappears from affected coverage. The current inventory guard checks
visible source routes and complete component/integration/platform function coverage.

Capture's component mode checks its isolated capture logic. `integration capture`
adds the real app/auth tests, message binding, final subscription commit and shared
restart checks. A runtime/lifecycle or trust-policy change needs its corresponding
integration/platform coverage before complete release acceptance.

Mixed modules select explicit test functions, preserving their parameterization.
A stale mixed-function declaration blocks the plan. Paths use POSIX separators on
every host, so a file is not repeated as both `tests\\file.py` and `tests/file.py`.
An empty feedback plan reports `no_tests_selected` and its deferred coverage; it
never invokes pytest with no selectors and accidentally runs the default suite.

Development checks include exact generated compact-artifact provenance so a stale
source hash is caught before the long release run.

The full mode forbids selectors, checks the exact SHA, and requires a clean checkout.
It strips inherited pytest filters and Python optimization, counts setup/teardown and
collection errors, checks that all collected tests passed and none were deselected,
and detects changed source before creating eligible evidence. `--allow-dirty` is an
explicit diagnostic mode, never accepted full-test evidence. No automatic retries,
xfail conversions, disabled assertions, or hidden test omissions are used.

The report's `full_test_evidence_eligible` flag means only that these complete test
requirements passed. It **does not authorize deployment** or certify Mesh rolling
compatibility. Release approval separately binds the exact source/tree, artifact,
full report, compatibility evidence, preservation guarantees and operator decision.
A short node check does not replace any of these.

The scoped cleanup separates the eight listed SQLite/Fleet/auth/agent/session/
Events/persistent modules. Pure rules, projections, router contracts and own SQLite
transactions are reviewed component checks; real app/auth/store bridges and native
shell/filesystem checks have separate physical modules. Extraction reuses assertions
and inputs. The current `task_context_ttl_seconds` replaces exercising the previous
`task_lease_seconds` alias. No migration was removed based on a versioned filename.

Current Python inventory: 271 modules, 1833 functions and 3044 collected cases.
The disjoint partition is 1175 component, 1679 remaining integration and 190 platform.
Integration includes its requested component checks. Capture stays 15 component /
28 explicit integration cases; Events component is 50 after promoting ten existing
managed EventStore cases. Full cardinality grew because existing loops/assertion
groups received atomic IDs, not new inputs.

Actual ASGI lifespan/auth checks need a genuine secure verifier-key file. Windows
cannot satisfy its POSIX mode check; the audit does not patch stat/permissions or
claim Linux acceptance. The native neighbor is doubled only in wiring checks that
do not test processes; native modules retain actual bash. Two symlink checks require
real creation/atomic replacement and remain platform checks. Preparation allowlists
retain the two relocated native files' former schema/empty-database reuse.

Affected plans traverse helper edges before intersecting final selectors with
`test_files(root)`. Consumers stay selected while `conftest.py` and other fixture-only
modules cannot become final pytest targets. The existing import-plan regression
covers both layers; no separate safeguard suite was introduced.

## Isolation, profiling and retention

Every run has separate temporary SQLite databases, runtime config, output, working
directory, sockets, Fleet metadata and Mesh state. Inherited `TERMINAL_MCP_*` settings
are removed. No test-state fallback points at production paths. The report stores
Python/dependency versions, exact argv and source identity, phase timings, per-test
outcomes, inclusive fixture setup times, CPU/RSS/load and SHA256 hashes of evidence.
Fixture totals are inclusive and must not be added together as exclusive wall time.
A fixture or helper invoked inside the test body remains part of pytest's call phase.

`--sample` enables a 50 ms read-only stack sampler. Samples from different threads
may overlap and are not additive elapsed time. Use identical instrumentation for
comparisons. `--fresh-preparation` exercises the original preparation paths without
selecting fewer tests. `--keep-state` retains successful run data for diagnosis;
otherwise only that run's successful temporary state is removed. Failed or timed-out
run state is retained, and existing report directories are never overwritten.

The `node` command uses the supported HTTPX client and performs only `GET /health/live`
with bounded timeouts. It verifies liveness and the public release string, rejects URL
credentials, and distinguishes a CDN challenge from an application HTTP failure.
It cannot establish the full source SHA, authenticated management functionality or
Mesh compatibility. The deployment workflow must additionally verify its local
artifact identity and configured MCP/OAuth/management routes.

## Preparation reuse and retained guarantees

The allowlists in `tests/conftest.py` are explicit. No runtime source is changed.
Behavior tests may reuse two kinds of preparation:

1. Immutable MCP schema dictionaries are built by the original generator, then stored
   in a bounded cache. Every consumer receives a deep copy. Function identity and
   generator arguments form the key; unknown/unhashable input uses the real generator.
   Static input and role-output model catalogs also reuse only model_json_schema
   dictionaries; BaseModel itself and unknown/dynamic models are never patched.
   Runtime validators, real HTTP/MCP adapters, processes and network boundaries remain
   unchanged. Contract/catalog/generator tests stay outside this allowlist.
2. Selected behavioral tests seed a new main SQLite database from a verified empty,
   current-version schema. SQLite's backup API seals the template; every application
   table must be empty. Publication is atomic and restricted to absent files inside
   that run's temporary directory. Existing databases are untouched. **The complete
   original initialize method still runs**, including migrations, validation, recovery
   and real transactions. Each database has independent contents and a separate inode.

Dedicated fresh-install, schema migration, restart/recovery, locking, durability,
process and TCP-partition tests retain their original storage preparation. The long
real-time Mesh timeout budget and inherited-FD stress tests are retained unchanged.
No sleep or fixture size was reduced merely to improve the reported total.

New regression tests compare cached metadata against original generator output,
verify independent nested values and restoration, execute real concurrent cache/seed
creation, check refusal to overwrite or escape the sandbox, reject nonempty templates,
and run real migration/restart recovery after seeding. Nested real pytest runs verify
that automatic allowlist activation works and `--fresh-preparation` disables it.

## Removed tests

The immutable-preparation optimization removed no tests. The later cleanup follows
the user's current policy: remove guarantees of previous import aliases/signatures
and retired peer-message scenarios, while keeping current authorization, slot,
storage and upgrade obligations. Each removal or replacement is recorded in
[test-scope-cleanup.md](test-scope-cleanup.md). Versioned filenames alone remain
insufficient evidence of retirement. Native ExecutionPort cases were moved intact
to a platform module, not deleted. Further mixed modules remain visible in the
integration/platform batches pending responsibility-focused refactoring.

## Benchmark protocol

Measure before and after on the same Secondary host and interpreter, serially, with
fresh state directories, the same hash seed and the same profiler. Record background
load and concurrent jobs. Use `--fresh-preparation` versus the default on the same SHA
for direct attribution; compare identical collected test IDs and outcomes. When a
new runtime candidate also changes, report its full-suite result separately rather
than crediting all difference to preparation reuse. Retain raw JSON/JUnit/log evidence.

Initial exact `8e78c905fd53b2ceab41a04f3005fa0fa0a881fe` reference on Secondary:
2748 passed, no skipped/xfail/failed tests; pytest 800.93 s, wrapper 803.73 s;
collection 10.68 s, setup 65.99 s, call 720.61 s, teardown 1.23 s. This is a baseline,
not a claim that the uncorrected candidate is safe to deploy. Subsequent measured
results belong in the task handoff alongside the exact tested source and raw artifacts.

Parallel test execution is not enabled. The existing suite contains shared process,
network and global-state boundaries whose independence has not been established for
all modules. These real checks remain part of every complete candidate acceptance.
