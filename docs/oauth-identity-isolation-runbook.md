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

## Controlled offline forward migration

1. Stage and verify the exact new release SHA. Stop terminal-mcp.service and
   ALL other workers that might write either database; confirm exit. Preserve
   a protected backup of runtime config/env, Access verifier key, Fleet databases,
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
key, configuration, Fleet state and release pointers. On restart the new app
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

