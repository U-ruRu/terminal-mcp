# Test workflow and reusable preparation

`python scripts/test.py` provides four levels of checks. Run it using the project's
installed test environment (`.[test,compact]`) and a checkout of the intended source.
It prints an external `report.json` path; results never disappear into terminal output.

## Commands

```sh
# Development feedback: public/compact artifact contracts, read/window/version and runner safeguards.
python scripts/test.py dev

# Component feedback, with documented transitive integration dependencies.
python scripts/test.py component mesh

# Inspect affected selection, including committed, staged, unstaged and untracked changes.
python scripts/test.py dev --changed-from <full-base-sha> --plan
python scripts/test.py dev --changed-from <full-base-sha>

# Candidate acceptance: one complete run on an exact, clean source checkout.
python scripts/test.py full --expected-sha <full-candidate-sha> --output /tmp/tmcp-acceptance-<unique-id>

# Read-only check of an already deployed node. Use its exact expected public release string.
python scripts/test.py node --url https://node.example --expected-release <version-shortsha>
```

`component` accepts `auth`, `mesh`, `storage`, `contracts`, `commands`, `slots`,
`events`, and `deployment`. The complete patterns and dependency graph are in
`scripts/test_plan.py`; `--plan` prints the actual file list and expansion reason.
For example Mesh includes auth, storage, contracts and events, including actual TCP,
lock, persistence and restart tests. Component feedback is not a release gate.

Affected selection deliberately favors safety: every application-source change,
shared contract, auth/Mesh/storage change, generator, installer, dependency lock,
frontend input, unknown file or deleted test expands to the **entire** test suite.
Only isolated test-file changes use a smaller selection. Their transitive test-helper
importers are found with AST analysis. An unresolved graph expands to all tests.
There is no claim of exact runtime impact inference from file names.

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

**None.** No evidence establishes that a currently collected contract has been retired.
Names such as `test_v08`, `test_v09` and `test_v010` are not a reason to remove regression
coverage. The optimization changes preparation rather than dropping assertions.

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
