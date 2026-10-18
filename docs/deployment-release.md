# Exact-candidate release workflow

The existing installer dispatches the supported workflow through
`bash deploy/install.sh release`. Run from a checkout of the exact approved workflow
source. Building and candidate acceptance belong on a build/acceptance worker; a
prepared target consumes the resulting immutable bundle and performs no full test run.

## Candidate preparation and evidence

```sh
bash deploy/install.sh release build --repo /workspace/terminal-mcp \
  --sha <40-character-candidate-sha> --baseline /secure/release-baseline.json \
  --output /secure/releases/<candidate-sha>

python scripts/test.py full --expected-sha <40-character-candidate-sha> \
  --output /secure/acceptance/<candidate-sha>

bash deploy/install.sh release approve --bundle /secure/releases/<candidate-sha> \
  --receipt /secure/acceptance/<candidate-sha>/report.json \
  --policy /secure/approval-policy.json --output /secure/approval.json
```

Use the same Python/native SQLite and every shipped service dependency for testing
and packaging. Build-frontend tooling may differ; service dependencies may not.
The approval gate checks full SHA/tree, a clean unchanged source, all outcome counts,
zero deselection, eligible full-test evidence, JUnit/profile/log hashes, Python/SQLite/
platform identity and the pinned runtime dependency set. A passing test report alone
never supplies operator approval, upgrade guarantees or Mesh compatibility evidence.

`build` reuses the existing installer staging function on a git archive of the exact
SHA. It neither uses floating HEAD nor changes a running release. Its bundle contains
manifest.json, dependencies.lock and payload.tar.gz. Runtime dependencies and payload
bytes are frozen in the bundle. The target does not repeat installation from an
unbounded package index. The current native artifact is platform-specific; a different
architecture/interpreter/libc is an explicit preflight blocker.

The approval policy binds source_sha, approved_by, evidence, accepted_exceptions=0,
required preservation guarantees, exact upgrade_from SHAs, and a Mesh policy with
recorded evidence. Review REQUIRED_GUARANTEES and validate_receipt in artifact.py.
Do not create a synthetic policy merely to get past a deployment blocker.

## Existing target prerequisites

A routine update requires an already provisioned node: existing service units, Caddy
routes, OAuth and management authorization, trust, explicit data paths and identity
assets. The profile command prints a non-secret inventory for operator review:

```sh
bash deploy/install.sh release profile --target <target> \
  --env-file /etc/terminal-mcp/terminal-mcp.env --root /opt/terminal-mcp
```

Store its reviewed output as root-owned, non-world-writable
`/etc/terminal-mcp/deploy-target.json`. Unit files and EnvironmentFile drop-ins are
included in the protected inventory. Profile drift is rejected on continuation.
No TLS, authorization, device pairing or Mesh trust changes are made by this workflow.

For an in-process executor, the coordinator must actually pause every command producer
before writing the short-lived root-owned admission guard referenced by the profile.
It binds target, source SHA, expiry and command_producers_paused=true. This is evidence
of an externally coordinated pause, not an implementation of atomic admission control.
A CLI confirmation string alone cannot establish that pause. Split deployments stop
the API, recheck unfinished commands, and only then stop the executor. A race blocks
progress and keeps the executor available for its outstanding command.

Mesh peer inventory and accepted previous versions are checked before mutation.
Rolling update requires affirmative compatibility evidence for every peer version.
An incompatible or unproven rolling plan stops before the first node is changed.
A coordinated order requires its separate maintenance window; the updater does not
manufacture compatibility evidence or automatically change trust to make it work.

## One routine command and its read-only variants

After the exact bundle, approval file and its independently pinned SHA256 are present
on the prepared target, use one command:

```sh
bash deploy/install.sh release apply --target <target> \
  --sha <40-character-candidate-sha> --bundle /secure/releases/<candidate-sha> \
  --approval /secure/approval.json --approval-sha256 <64-character-approval-sha256> \
  --confirm-window <target>:idle-window
```

Replace `apply` with `plan` and omit --confirm-window for the non-mutating preflight.
Replace it with `status` and omit --confirm-window to inspect recorded progress without
service changes. Status is a local operation-state inspection, not a fresh release
acceptance. Existing completed operations return their known result without rerunning
migrations. A timed-out call is not permission to issue a second mutation.

A first installation on already provisioned infrastructure uses `initial` explicitly.
It requires pristine stores and refuses an existing release. Initial node provisioning,
secret entry, peer trust and service/Caddy setup remain separate operator actions.
The updater refuses missing prerequisites before starting a half-upgrade.

## Stages and short node checks

The operation journal is written before each mutation. Preflight checks platform,
current release, explicit schema versions, protected identity assets, idle state,
coordinated admission pause, disk reserve and peer compatibility. A new release is
staged separately. The API/executor are stopped in the supported topology; SQLite's
backup API snapshots every configured store consistently while services are offline.
Protected configuration and identity sidecars are inventoried and hashed privately.

Migrations run using the staged native runtime, with a durable started marker and
completion receipt. Existing durable rows and protected configurations are checked
against the snapshot. The switch and service start are followed by bounded HTTPX
liveness/version and Caddy boundary probes: MCP, OAuth discovery, Access management,
and enabled persistent management paths. A CDN challenge is classified separately
from an application failure. Public 401 checks prove routing/authentication boundaries;
they do not claim an authenticated management acceptance test.

Results distinguish `applied`, `not_applied`, and `partially_applied`.
An unreadable status/recovery context or interrupted migrator reports
`partially_applied` with `state_unknown=true`; it never asserts that nothing changed. Partial application
has a nonzero exit status. Inspect its private journal and completion receipt; do not
paste raw configuration, token databases or keys into chat or public logs.

## Recovery runbook

Run the same pinned `status` command first. Keep the original bundle/approval/profile
and operation identity. Do not remove a `.started` marker or manually reset a journal.

Before migration starts, `recover --confirm-window <target>:idle-window` can restore
the previous release link/service after examining the saved stage. It does not restore
old database snapshots over current user writes. An explicit new attempt number is
allowed only after that pre-migration operation has been safely recovered.

At or after migration, returning the old symlink is not a schema rollback. An interrupted
migrator with no valid completion receipt is an unknown outcome and blocks blind replay.
Inspect the actual schema, migration marker, protected-store snapshot and journal.
Escalate an incomplete migration for a reviewed forward repair; retain all evidence.

A completed migration whose caller timed out can be reconciled from its durable receipt.
`resume --confirm-window <target>:idle-window` verifies the receipt/state and proceeds
forward through switch/start/probes without rerunning migration. A startup or health
failure after a completed migration likewise uses forward-only recovery. Any restoration
from backup requires a separate operator decision that accounts for newer writes; the
workflow never automatically overwrites them with an older database.

## Acceptance boundaries

Journal/fault tests cover initial/update/idempotence, schema/space/Mesh blockers,
active commands, stage/backup/start/probe failures, timeouts with unknown outcomes,
read-only status, concurrent operation locks, evidence tampering and safe recovery.
Real-process migration tests additionally exercise actual application/store constructors,
fresh identity creation and a second migration preserving keys, rows and fixed deadlines.
SQLite WAL, loopback HTTP and subprocess timeout boundaries run for real. Service
actuation in the fault matrix is an explicit isolated adapter; it is not a claim of
physical systemd/Caddy deployment acceptance. A production trial still requires dot's
specific approval, real target preflight and confirmed compatibility evidence.

## Fresh installation and explicit state reset

`bash deploy/install.sh install` requires an empty managed installation state.
Use `update` or `release apply` to preserve the whole installation, or explicitly
choose a clean deployment.

**Default clean deployment:**

    bash deploy/install.sh clean-deploy

After staging and validating the new release, this clears operational runtime
databases, cache, backups, Mesh state and old runtime configuration. It retains
the OAuth authorization database (including WAL, SHM, journal, access key and
identity anchor), ChatGPT client registrations and grants, refresh tokens,
signing/session secrets, administrator credentials and OAuth username/password
files. Existing Console device pairing also survives by default. No new pairing
URL is issued. Existing administrative password and trust domain are reused:
attempted implicit authentication or issuer/public-origin rotation is rejected.

**Explicit full authorization reset:**

    TERMINAL_MCP_ADMIN_PASSWORD=... TERMINAL_MCP_OAUTH_USERNAME=... TERMINAL_MCP_OAUTH_PASSWORD=... bash deploy/install.sh clean-deploy --reset-auth

The old auth database, sidecars, OAuth credentials, clients and tokens are
removed; fresh secrets and credential files are created. Old tokens stop
working. New admin and OAuth username/password inputs are mandatory before
any destructive action.

**Explicit device pairing reset:**

    bash deploy/install.sh clean-deploy --reset-pairing

This revokes paired-device clients and refresh tokens and removes device and
pending-pairing records while preserving ChatGPT OAuth clients. After the
new release is ready, the installer creates a one-time Console pairing URL
valid for **10,800 seconds (three hours)**. It prints the link to the
operator and saves it to `/etc/terminal-mcp/pairing-url.txt` (or the configured
environment directory), with root-only `0600` access. An authorized server
agent can read this file to give the link to the user. Treat the link as a
bearer secret: anyone holding it can use it once before expiry. An expired or
consumed link must be reissued explicitly. Both `--reset-auth` and
`--reset-pairing` can be used together; the pairing URL is created **only**
when `--reset-pairing` is specified.

Both reset switches are exclusive to `clean-deploy`. For a new installation
without a preexisting auth database, supply `TERMINAL_MCP_ADMIN_PASSWORD`,
`TERMINAL_MCP_OAUTH_USERNAME` and `TERMINAL_MCP_OAUTH_PASSWORD`.
Old auth secrets/credential paths are read from `terminal-mcp.env` and any
configured `runtime.env`, including custom locations. Retained files remain
in place with root-only access; only the allowlisted authorization values
are transferred to the new environment. Runtime, Mesh and slot configuration
are reset. Symlinks, malformed or incomplete auth state, mismatched identity
lineage, and unauthorized trust-domain changes fail before state deletion.

**Recovery:** There is no automatic old-release rollback after clean cleanup.
If interrupted, keep the retained environment and protected auth SQLite and
sidecars intact; rerun ordinary `clean-deploy` after correcting the reported
failure. A partially completed pairing reset may be rerun with
`--reset-pairing` to issue a new link. Never use `--reset-auth` as a recovery
shortcut; it irreversibly invalidates prior OAuth clients and tokens.

### Standalone Console ingress acceptance

The common Console `/connect`, snapshot preflight and unauthenticated
WebSocket-ticket endpoints are required on both Mesh and standalone nodes.
The `/console/fleet/v1/*` projection API is optional: standalone nodes
without the local Fleet projection return `404`, while Mesh nodes exposing
that projection return `401` to unauthenticated requests. Deployment checks
must first inspect the local `/console/fleet/v1/probe` route. If local is
`404`, require public `404` and skip the other absent projection routes. If
local is `401`, require public `401` on all projection routes. Any other
local status or local/public mismatch fails activation, preserving the
reverse-proxy security check on actual Mesh deployments.
