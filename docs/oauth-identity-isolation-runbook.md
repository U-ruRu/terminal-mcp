# OAuth identity isolation and clean-start runbook

Status: isolated implementation candidate. Live FirstByte/BacLOUD rollout requires separate approval.

## Scope and original incident

Before this fix OAuthStore and PairingStore were wired to the disposable
terminal-mcp.sqlite3 instead of the existing protected auth.sqlite3.
FirstByte clean start erased the registered ChatGPT clients, refresh grants,
and Android/console pairings. See the incident report under
/workspace/terminal-mcp-artifacts/whiskey2-release-20261010/OAUTH-INCIDENT-FIRSTBYTE.md.

The six relations are indivisible: oauth_clients (IDs, secret hashes, redirect
URIs, auth methods), oauth_codes (PKCE/expiry/use state), oauth_refresh_tokens
(token hashes, scope, expiration, revocation), console_pairings (secret hashes,
expiry, consumption), console_devices (device IDs, clients, public keys, last
use, revocation), and console_pairing_audit (IDs, timestamps and events).
PairingStore.exchange updates multiple relations in one transaction. The
existing AuthFoundation principals, grants, Access verifier key, auth realm,
and signing configuration must also be retained. These existing records are
already separate from the working SQLite database.

After forward migration auth.sqlite3 is the ONLY active source for these six
relations. The runtime fails closed when any legacy identity table still exists
in its working database. Paths must be physically distinct, including hardlinks.

## Binding protected identity to this installation (B3 continuity)

The protected database, existing Access verifier key and independent
identity-anchor file are a three-part installation identity. The new file
is adjacent to the protected database (AUTH.identity-anchor, for example
auth.sqlite3.identity-anchor), outside both the replaceable auth SQLite file
and the disposable working database. It contains a random 256-bit installation
nonce with a format identifier. It is created exclusively (O_EXCL), mode 0600,
and never overwritten by bootstrap, migration, clean, or ordinary restarts.

The migration-completion row contains the same nonce and an HMAC-SHA256 proof
keyed by the pre-existing Access verifier key. Every startup, inspect,
idempotent migration and clean verifies: (1) the complete protected Foundation
and OAuth/console schema; (2) its completed migration marker; (3) equality of
the protected database nonce with the external immutable anchor; and
(4) the proof under that installation's existing verifier key. A foreign
fully-initialized auth SQLite fails even if its schema is identical, its
clients are empty and it was initialized under the SAME verifier key. No
OAuth credentials, keys, grant permissions, token expiry or rotation are
changed by this provenance check.

A legitimate first install has no prior protected DB, verifier key or anchor,
and has either no runtime database or only empty constructor-created Access
Mesh metadata. Foundation creates its standard verifier key, then fresh
identity bootstrap exclusively creates and seals the anchor and marker before
the server accepts traffic. Presence of historical state forbids silently
regenerating an installation identity.

An offline forward migration first verifies existing protected Foundation and
all legacy identity relations, saves recoverable SQLite snapshots, then
exclusively creates the installation anchor and authenticates the new marker
in the attached transaction. If a migration is killed before COMMIT, the
legacy source remains intact. A fully written pending anchor is reused by a
subsequent offline forward retry; it cannot be overwritten with a new nonce.
An incomplete anchor fails closed and requires reviewed recovery of the
pre-migration state rather than an automatic reset.

Keep the protected auth SQLite, the Access verifier key, the immutable
identity anchor, and config together as a single backup and recovery unit.
The authoritative backup inventory includes the external anchor when
present or required by the auth marker. Clean's quarantine backup also
preserves it independently. After an authorized restore, verify the
database, key and anchor all belong to the same installation and inspect
reports isolated before permitting startup or clean. If the anchor is
missing, malformed, permissions-insecure or mismatched, stop and recover
the correct anchor and auth from a consistent trusted backup; never copy
a new foreign DB over the existing one or adopt its generated nonce.

Inspect differentiates new-install, legacy-present (pre-migration Foundation
verified), isolated, missing-protected-auth, incompatible-protected-auth,
untrusted-protected-auth and conflicting-identity-sources. Legacy-present
does not authorize clean.

An older snapshot from the SAME installation may still carry a correct
anchor and HMAC yet precede later token rotations or revocations. Identity
provenance does not establish freshness of mutable authorization records.
The restore runbook must preserve the current auth state and require an
explicit operator review of changes made after the snapshot; it must not
silently replace current live auth. A general mutation-synchronized
anti-rollback authority needs separate product design and is outside
this repair. No credential rotation is performed here.

## Controlled offline forward migration

1. Stage and verify the exact new release SHA. Stop terminal-mcp.service and
   ALL other workers that might write either database; confirm exit. Preserve
   a protected backup of runtime config/env, Access verifier key, Mesh databases,
   service release metadata and signing configuration. Never log secret values.
2. Set PY to the candidate Python executable containing the migration module,
   MAIN to absolute working database path, AUTH to absolute protected auth
   database path, OUTPUT to absolute output cache path, BACKUP_DIR to an
   external protected backup root. Use new per-operation subdirectories.
3. Inspect, transfer, and inspect again:

~~~bash
"$PY" -m terminal_mcp.auth.identity_migration inspect --main "$MAIN" --auth "$AUTH"
"$PY" -m terminal_mcp.auth.identity_migration forward \
  --main "$MAIN" --auth "$AUTH" \
  --backup-dir "$BACKUP_DIR/migrate-$(date -u +%Y%m%dT%H%M%SZ)" \
  --service-stopped
"$PY" -m terminal_mcp.auth.identity_migration inspect --main "$MAIN" --auth "$AUTH"
~~~

The tool snapshots both SQLite databases using SQLite online backup into
mode-0600 files inside a mode-0700 directory BEFORE changing records. A
single rollback-journal ATTACH transaction copies the six tables, verifies
schema and content fingerprints, drops their old source copies, and creates
the auth indexes. Incomplete schemas, populated destinations, or existing
migration markers fail explicitly; no INSERT OR REPLACE, merge, regenerated
credentials, privilege changes or token-expiry changes. Interrupted writes
roll back; a completed call is safe to repeat. Keep snapshots out of Git.

4. Check main/auth PRAGMA quick_check and foreign_key_check; table counts and
   hashes, original OAuth client and redirect URI registrations, Android
   device links and revocation timestamps, access scope, expiring grants.
   Review locally without printing token values. Preserve the current
   30-day refresh TTL (2592000 seconds) and any explicit rotation settings.
5. Start the NEW binary and validate both previously connected ChatGPT and
   Android clients with their existing credentials, through actual endpoints
   and device apps. Verify synthetic expiry and revocation. Run release/QA
   gates before live approval.

## Approved clean start: task/message/command/output data only

After a completed migration, stop all writers. The shipped clean action
creates a consistent pre-clean snapshot of auth.sqlite3 and moves main SQLite,
output-cache SQLite and their WAL/SHM sidecars into a NEW protected quarantine:

~~~bash
"$PY" -m terminal_mcp.auth.identity_migration clean \
  --main "$MAIN" --auth "$AUTH" --output "$OUTPUT" \
  --backup-dir "$BACKUP_DIR/clean-$(date -u +%Y%m%dT%H%M%SZ)" \
  --service-stopped
~~~

Clean refuses stale legacy OAuth tables, missing auth storage, path aliases,
or an existing quarantine directory, and restores moved working files if an
intermediate move fails. It preserves auth.sqlite3, any auth WAL/SHM, Access
key, configuration, Mesh state and release pointers. On restart the new app
recreates fresh working data and output cache. The old clients remain valid.
NEVER delete the entire working directory, protected auth or key material.

## Rollback after migration or later credential changes

* Before migration commit: no identity source switched. Preserve backups,
  investigate conflicts, and retry offline with a fresh backup directory.
* After migration, before user activity: old binary expects identities in
  the working DB. Restore a compatible old runtime schema into a SEPARATE
  isolated working copy, back it up, then explicitly remove its stale six
  identity tables. With an EMPTY identity destination, use reverse (below).
* After user activity: first stop all writers and back up BOTH CURRENT
  databases again. Prepare an old-schema working copy with no identity rows,
  then reverse from the CURRENT auth.sqlite3, transferring post-cutover
  refresh rotations, newly created credentials, device state and revocations.
  Reverse refuses a populated working identity destination. Never silently
  merge older backup rows with current auth.
* Restart old binary ONLY after reverse and old main schema compatibility are
  checked; never restore pre-cutover auth snapshots over newer auth records.
  Old code then owns identity in main again. Repeat forward with fresh
  backups before trying the new release a second time.
* To undo only working-data clean (same new binary), restore quarantined
  runtime/output SQLite files while stopped; KEEP CURRENT auth.sqlite3.

~~~bash
"$PY" -m terminal_mcp.auth.identity_migration reverse \
  --main "$OLD_COMPATIBLE_MAIN_WITHOUT_IDENTITIES" --auth "$AUTH" \
  --backup-dir "$BACKUP_DIR/reverse-$(date -u +%Y%m%dT%H%M%SZ)" \
  --service-stopped
~~~

Old and new binaries must never concurrently write different identity stores.
Live validation and independent QA are separate deployment gates.

## B1–B4 release-blocker closure (candidate for independent QA)

The forward migration refuses a missing or uninitialized protected auth.sqlite3
(B1). It requires pre-existing AuthFoundation schema version 3, the core
principal/grant tables, security generation row and protected verifier-key
file. Missing protected state is a recovery incident: restore it from its
verified backup while stopped. Do not run forward against a new blank file.

Existing deployments also require all six auth identity tables AND a single
completed oauth_identity_migration marker. Startup and clean start run this
read-only preflight BEFORE initializing any runtime worker or moving working
data; empty/replaced protected auth fails closed (B3). The sole new-install bootstrap path requires no protected auth DB or Access
verifier key and either no runtime DB or only empty, pre-initialized
access_mesh_* metadata tables (created by constructor composition). After its three identity stores initialize, startup
writes the completed marker before serving requests. The operator must verify
that no prior configuration or backup is being abandoned before approving a
new installation.

Before any transfer the tool validates the complete table definitions and
required explicit indexes, including UNIQUE(console_devices.client_id),
CHECK, defaults and AUTOINCREMENT, of source and pre-existing target (B2).
Incompatible destinations are refused, even if empty. Transfer preserves
the AUTOINCREMENT high-water mark of console_pairing_audit (B4), including
previously issued IDs that have since been deleted, in both forward and
reverse transactions. As before, credentials and grants remain unchanged.

The quarantine destination must reside on the same filesystem as both main
and output SQLite files and their sidecars, so every rename is atomic. The
shipped clean action checks this before moving files. Verify this condition
before the release window; otherwise arrange a separate filesystem-local
quarantine/restore procedure approved by the release owner.

All four negative scenarios must pass on the final exact SHA and receive
independent QA acceptance before integrating or deploying this patch.

## First ChatGPT login after standalone reset

ChatGPT may send a newly generated public OAuth `client_id` on
`GET /oauth/authorize` even when the fresh standalone database has no client
registrations. A missing client is **not** a reason to reject an otherwise
well-formed login request if the redirect is an exact HTTPS ChatGPT connector
callback (`https://chatgpt.com/connector/oauth/<connector-id>`) and the S256
PKCE challenge is valid.

The GET renders the username/password form without creating records. On POST,
the owner's configured OAuth credentials and requested scopes are verified
first. Only then does the server atomically bind that client ID to the exact
callback and issue a code. A mismatched existing client binding cannot be
replaced. Malformed IDs, other hosts, redirects with queries, invalid PKCE and
failed logins do not create registrations. Existing registered clients and the
normal `/oauth/register` workflow are unchanged.

The login still requires root-owned `0600` files
`/etc/terminal-mcp/credentials/oauth-username` and `oauth-password`. A clean
deployment should validate the presence of these credentials before expecting
interactive OAuth login to succeed. Neither copying an old OAuth database nor
pre-inserting a row per ChatGPT connector is necessary.
