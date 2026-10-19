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

## Console scope correction

The global Slots screen now has one registry context for each observed Mesh and
one for each observed Standalone. A member's own fresh control observation
determines its actual scope. A desired membership row from another control does
not override an observed Standalone. Unknown connections are not management
targets.

Slots was removed from server workspace navigation. The global Slots entry and
the Mesh Slots entry remain. An existing member slot route replaces itself with
the corresponding Mesh route before loading native slots or defaults. Existing
Standalone routes still address their own independent scope.

A Mesh uses one available ingress for its reads and mutations. A pending operation
prefers its original paired device; journal identity includes the registry and
the pairing. Explicit `not_committed` failures clear the pending operation.
Late responses from an old registry cannot confirm or remove data in a new one.
Adding an unrelated registry no longer clears existing observations or cancels
another target's pending defaults request.

Mesh policy writes use the existing fleet policy action through a member or the
authority. The native authoritative parent revision must match that ingress's
fresh control policy revision. A lost receipt, including a structured
`control_mutation_unconfirmed`, is confirmed only by a newer exact policy read
from the original Mesh. Standalone policy keeps its local defaults action.
No alternate agent methods, compatibility modes or migrations were introduced.

## Scoped Console verification

The first Console freeze passed 40 atomic cases across eight existing owners in
8.09 seconds, followed by TypeScript and Vite production build:

```sh
npm test -- src/components/AppShell.test.tsx \
  src/AppSlotsNativeWiring.qa.test.tsx \
  src/routes/ServerSlots.test.tsx \
  src/routes/MeshSlotPolicyTransitions.test.tsx \
  src/routes/SlotAuthorityTransition.qa.test.tsx \
  src/slots/accessMeshState.test.ts \
  src/slots/accessMeshReadIsolation.test.tsx \
  src/slots/operationJournal.test.ts
npm run build
```

The final navigation and policy-response scope changes were checked by rerunning
only their two owners: `AppShell.test.tsx` and
`MeshSlotPolicyTransitions.test.tsx`, with 11 passed in 3.50 seconds. That selector
reuses ten cases and adds one foreign-Mesh receipt case, giving 41 distinct
Console cases across the bounded selectors, not 51 distinct cases.

The old assertion that an independently paired primary timeout always makes
policy read-only was updated to check the missing matching authoritative parent
revision. Member forwarding is an existing supported operation; authorization,
CAS, freshness and draft-preservation checks remain. No new test owner or broad
suite was added. Independent reviews checked registry identity, pending journal
ownership and stale response handling.

The final read-state hook cleanup updates callback guards during layout commit and
prunes cache only when target identities change. It does not disable lint rules.
The three affected existing owners (`accessMeshState.test.ts`,
`accessMeshReadIsolation.test.tsx`, `SlotAuthorityTransition.qa.test.tsx`) passed
14 reused atomic cases. Scoped ESLint with `--max-warnings=0`, TypeScript with
`--noEmit -p tsconfig.app.json`, and `git diff --check` then passed.

## Live update and operator verification

Backend commit `91bdf6bb7b3e5016ad0c929c73c7649780948710` was archived once and
installed through the data-preserving update procedure on all four nodes.
Archive SHA-256:
`1372700f9b70b26e58368bf3784a73093aecf3b40ea64807f5fd57f0af2e05b0`.

| Node | Running release | Mesh role |
| --- | --- | --- |
| BacLOUD | `0.29.3-91bdf6b` | Authority |
| FirstByte | `0.30.3-91bdf6b` | Member |
| Solvenger Main | `0.29.3-91bdf6b` | Member |
| TokyoHH | `0.15.3-91bdf6b` | Member |

All four update units completed successfully and extended health reported
healthy. The ordinary Tokyo release, environment hash and registry scope were
compared before/after and remained unchanged. It is still Standalone on
`0.29.0-4dbffe9`. Secondary was not accessed.

The same legitimate temporary Console devices were used before and after the
update. The following individual live actions succeeded:

| Action | Ingress | Result | Trace ID |
| --- | --- | --- | --- |
| Create an unused temporary probe slot | FirstByte | HTTP 200, `ok=true`, revision 1 | `01a152e2-6dba-7c9b-b6d4-94279f888924` |
| Set that slot's policy | TokyoHH | HTTP 200, `ok=true`, revision 2 | `01a152e4-108e-77a5-aa06-752580158ac4` |
| Save existing shared policy values | Main | HTTP 200, `ok=true`; values and revision 1 preserved | `01a152e6-490d-7f4f-bc5a-a6bcc16e8133` |
| Delete only the probe slot | Main | HTTP 200, `ok=true`, deletion receipt revision 3 | `01a152eb-4b70-71e4-b14b-b1623de33195` |

At 06:49 UTC all four nodes returned successful control, slots and defaults
reads. They agreed on the BacLOUD authority, Mesh ID, topology revision 5, trust
revision 4 and policy revision 1. Every node had three verified peers, an
authoritative converged Mesh policy parent, and `write_target=fleet_control`.
The probe slot was absent from all four lists. The original shared policy values
were preserved. Actual MCP terminal commands also ran successfully after the
TokyoHH update using the existing logical agent binding.

## Signed APK and cleanup

Console source commit: `7e35d0a78c9699086c60718e308103575bc2a084`.
The release build completed with the existing Java 21 / Android SDK toolchain.
`android:verify:release` verified package `app.terminalmcp.console`, version
`0.2.45` / code `47`, and the existing signing certificate.

APK SHA-256:
`4dded2d6d5d413172ebe9340e7309a74d3410fc06dd17f9c374d70a463c2c3b5`.
APK size: 11,477,259 bytes.
Signing certificate SHA-256:
`aa9207e1051aa54c71421f02609ab8be6074ebf50963af59b6474a16d0d6e1c6`.

The APK and update manifest were published on BacLOUD after checking that the
previous manifest had not changed. The previous manifest was retained in the
operational backup. A normal HTTPS download of both public files confirmed the
source commit, version and exact APK hash:

https://terminal-bc.elenis.org/downloads/android/terminal-mcp-console-0.2.45-47-7e35d0a.apk

Only the four temporary Console devices created for this investigation were
revoked. On each node the original device identity and OAuth client remained,
all three identity sidecar hashes matched the backup, and the active device
count returned to one. The temporary devices' OAuth rows and private probe files
were removed. Temporary signing material on TokyoHH was removed after publishing.
Extended health on all four nodes remained healthy with no running, stale or
unowned terminal commands. Ordinary Tokyo was unchanged; Secondary was not used.

The Android package was built and signature-checked; it was not installed or
manually exercised on the user's physical phone in this session.
