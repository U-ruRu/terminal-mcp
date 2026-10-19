# Mesh slot and policy controls — 2026-10-19

## Scope and baseline

Canonical baseline: `481e0caf3cb554b99f20f225b0f3fee3b12f4f99` on
`integration/M3-functional-candidate`, fetched on TokyoHH before editing.

The requested current topology is BacLOUD authority with FirstByte, TokyoHH and
Solvenger Main in `mesh-5d2c606c472d349b7fae3ae3f6c79675`.
Ordinary Tokyo remains Standalone. Secondary is excluded from this work.

## Confirmed operator failure

At 06:19 UTC, a temporary Console device paired through the normal one-time link
and `/pairing/exchange` on each of the four current nodes:

- BacLOUD returned successful `/actions/access/slots` and
  `/actions/access/defaults`.
- FirstByte, TokyoHH and Main returned HTTP 200 with
  `ok=false, code=identity_metadata_missing, outcome=not_committed, retry=repair`
  for both reads.
- All four shared the same registry namespace and BacLOUD authority, with no
  transition or upgrade pending. Existing phone pairing count was one per node.
- Current shared policy was preserved: duration 1380 seconds, warning after 1200,
  alert after 1320, cooldown 180, Rearm enabled and Legacy disabled.

The member's registry proxy called agent `connection_key(actor)` for an admitted
Console operator, whose OAuth principal does not have agent provider metadata.
The error happened before the peer request reached the authority.

## Backend correction

The existing registry envelope carries a null connection binding for admitted
operator operations `slots/get/defaults/mutate`. The authority accepts that
binding only with the operator role and those operations. Explicit and automatic
agent sessions retain the required 64-hex connection binding and existing role
and argument restrictions.

Admission/scopes, trusted peer, namespace, registry availability and transactional
fences remain in place. No public methods, database migrations or compatibility
mechanisms were added. Mesh policy still uses `/actions/fleet/control/policy`;
Standalone policy still uses its local defaults mutation.

## Atomic source verification

New cases reuse `tests/test_application_fleet_boundaries.py`, already registered
as a component owner. Neighboring store, transport and handler ports are doubled.
The four new test functions cover 11 cases:

- Operator proxy without agent metadata: four operator operations.
- Correct operator/agent dispatch binding and context restoration: two cases.
- Crossed operation/role/binding rejection: four cases.
- Agent proxy still requires connection identity: one case.

The first selector failed all four cases against the unmodified baseline in
0.86 seconds, reproducing the failure before the production patch was applied.

After applying the patch:

```sh
.venv/bin/python -m pytest -q tests/test_application_fleet_boundaries.py \
  tests/test_auth_path_policy.py::test_paired_console_slot_mutation_requires_execute_scope
.venv/bin/python -m ruff check src/terminal_mcp/application/access_mesh_registry.py \
  tests/test_application_fleet_boundaries.py
git diff --check
```

Result: **15 passed in 0.56 seconds** (1.25 seconds command wall time), Ruff passed,
and whitespace checks passed. The 15 cases comprise 11 new and four reused cases.
No existing tests were removed or changed, and no comprehensive suite was run.
The prepared Python 3.14 environment emitted existing pytest-asyncio deprecation
warnings. Independent review found no blocker in the role/binding boundary.

## Operational recovery point

Before the operator probes, SQLite snapshots, configuration and identity sidecars
were saved on each of the four nodes under
`/var/backups/terminal-mcp/slot-scope-20261019T0620Z`, with private permissions.
The backup inventory records original device rows and identity file hashes.
Production data was not reset.
