# Console slot editor stability — 2026-10-19

Baseline: `d028d1c18019621f8b1a1ea3657c8114a6787b24` on
`integration/M3-functional-candidate`.

Work branch: `fix/console-slot-editor-stability-20261019`.
Console release: **0.2.46 / versionCode 48**.

## Behavior

- The server workspace's Slots button opens `/slots`, with the common Mesh and
  Standalone selector.
- Each direct control observation belongs to its pairing. Transient transport
  status, another profile's status, and replacement loader callbacks do not clear
  the registry or restart its policy read.
- Control and policy load on entry, explicit refresh, and an operator action.
  The 15-second/visibility refresh reads only slots. A pending defaults read can
  complete independently while new slots appear.
- Policy forms retain their own complete draft. Polling does not replace inputs,
  disable them, change focus, or introduce transient status blocks above them.
  Cancel explicitly restores the last read values; confirmed Save applies the
  confirmed policy returned by the action owner.
- Descriptive policy/create text and inline save results are removed. Local
  validation and action outcomes use the existing fixed overlay toast.
- New mutations read current scope and revision after the user's action, then
  apply the complete captured draft. A definite CAS rejection permits one retry
  with current metadata; a native retry has a new idempotency key. A persisted
  uncertain native operation retains its exact original request and key.
- Mesh confirmation accepts an unchanged policy at the same revision, provided
  its scope and all six policy values match. Unknown outcomes are read back once
  without automatically repeating the mutation.
- Pairing, registry, permission, management-contract, and session-window identity
  checks remain. This change adds no backend API or old-server compatibility.

## Atomic test ownership

Existing owners are reused; no new test module or runner is introduced.

| Owner | Changed responsibility |
|---|---|
| `components/AppShell.test.tsx` | Server Slots navigation opens the common selector. |
| `routes/SlotWindowAuthority.test.tsx` | Inheritance reset preserves user intent across a parent update; shortening remains locally rejected through the toast callback. |
| `slots/accessMeshState.test.ts` | An unrelated target or transient connection status preserves the same registry's cached rows and pending defaults. |
| `slots/accessMeshReadIsolation.test.tsx` | A real 15-second scheduler tick updates slots without restarting or cancelling pending defaults. |
| `routes/ServerSlots.test.tsx` | Action-time revisions, retained rows and stable policy editing. |
| `routes/MeshSlotPolicyTransitions.test.tsx` | Action-time Mesh CAS and scoped confirmation of known or uncertain outcomes. |
| `routes/SlotAuthorityTransition.qa.test.tsx` | Pairing/registry fencing and immutable recovery remain at the existing consumer boundary. |

The former expectations that background freshness blocks editing, a stale draft
must be manually discarded, or server navigation has no Slots entry are retired.
Their current replacements keep scope/CAS admission inside the explicit action.

## Verification

Commands run in `apps/console` on Tokyo HH's existing isolated checkout:

```sh
npm run test -- src/slots/accessMeshState.test.ts src/slots/accessMeshReadIsolation.test.tsx
npm run test -- src/slots/accessMeshReadIsolation.test.tsx
npm run test -- src/components/AppShell.test.tsx src/routes/SlotWindowAuthority.test.tsx
npm run test -- src/routes/ServerSlots.test.tsx src/routes/MeshSlotPolicyTransitions.test.tsx src/routes/SlotAuthorityTransition.qa.test.tsx
npx tsc --noEmit -p tsconfig.app.json
npx eslint src/components/AppShell.tsx src/routes/AccessMeshPolicyControls.tsx src/routes/MeshAccessPolicyControls.tsx src/routes/ServerSlots.tsx src/slots/accessMeshState.ts src/slots/meshPolicy.ts --max-warnings=0
npx eslint src/routes/ServerSlots.tsx src/routes/ServerSlots.test.tsx src/routes/MeshSlotPolicyTransitions.test.tsx src/routes/SlotAuthorityTransition.qa.test.tsx src/components/AppShell.test.tsx src/routes/SlotWindowAuthority.test.tsx src/slots/accessMeshState.test.ts src/slots/accessMeshReadIsolation.test.tsx --max-warnings=0
```

- Hook owners: **5 unique cases pass**. The first scheduler test attempt used
  `waitFor` with its own interval mocked; replacing that harness wait with `act`
  resolved the harness failure. The focused rerun passed in 1.55 seconds.
- Navigation and direct form owners: **5 cases pass**, 2.52 seconds.
- Route owners: **34 cases pass**, 4.94 seconds. The initial run identified one
  real-registry transition whose obsolete selector key hid the new registry. The
  fallback now applies only when that context actually disappears; the unchanged
  owner and the other two affected route owners all pass after the source fix.
- Total: **44 unique atomic cases across seven existing owners**. Repeated
  diagnostic runs are not counted as additional cases.
- Application TypeScript: **pass**.
- Changed production/test files ESLint: **pass**, zero warnings.
- `git diff --check`: **pass**. All 16 changed files were byte-verified between the
  reviewed local snapshot and the tested remote checkout before the evidence-only
  update to this report.
- Canonical fetch at 08:13 UTC still resolved to baseline `d028d1c`.

This is a scoped atomic run. No full, integration, platform, or backend suite is
claimed. No device installation or physical Android keyboard behavior is claimed
by DOM tests. Android release packaging and signature verification are separate
release checks.

## Release scope

The release is an APK update. Runtime code and configuration on the four Mesh
servers are unchanged by this follow-up. Ordinary Tokyo and Secondary are not
modified. The existing release signing identity is reused; task-local signing
material is removed after publication.

The canonical ref is fetched again before integration. Source is built from the
resulting committed revision, and the published manifest identifies that exact
revision. APK signature, package, version, public download hash, and manifest are
verified before handoff.

## Published artifact

Source commit: `1ce207a4fbf9cf2ccc6df0f4330dcf67ec3629a8`. The normal
fast-forward canonical push completed after a fresh fetch of `d028d1c`; the
checkout was clean throughout the release build.

[Console 0.2.46 (48) APK](https://terminal-bc.elenis.org/downloads/android/terminal-mcp-console-0.2.46-48-1ce207a.apk)

| Check | Result |
|---|---|
| `npm run android:build:release` | PASS, 35.23 seconds; includes application TypeScript, Vite, Capacitor sync, branding verification and Gradle release packaging. |
| `npm run android:verify:release` | PASS, 1.53 seconds. |
| Package | `app.terminalmcp.console` |
| Version | `0.2.46`, code `48` |
| APK size | `11,478,455` bytes |
| APK SHA-256 | `5886032f9111eb4d2f66463fd10b4371c5543fa79de99fa06ad4f784429fcda3` |
| Signing certificate SHA-256 | `aa9207e1051aa54c71421f02609ab8be6074ebf50963af59b6474a16d0d6e1c6`, unchanged from release 47. |
| Public verification | At 08:21 UTC, ordinary HTTPS downloads of both the APK and `latest.json` exactly matched the verified local artifact and manifest. |

Publication preserves the previous manifest and checks it for concurrent changes.
The first rename from `/run` to `/var` was rejected across filesystem boundaries
before changing the public manifest. The completed publication stages temporary
files on the destination filesystem and atomically replaces them there.

Only this task's temporary signing copies and publication staging files are
removed during release cleanup. The original signing material remains unchanged.
