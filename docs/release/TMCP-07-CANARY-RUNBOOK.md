# TMCP-07 — FirstByte/BacLOUD canary runbook (operator only)

Status: PREPARED, NOT EXECUTED. Separate release approval is mandatory.
Do not deploy, rotate tokens, generate credentials, grant permissions or alter
Canonical/runtime as part of TMCP-07 integration.

## 0. Hold points / handover

Only proceed after dot records final integration SHA, remote equality,
independent generator acceptance, deterministic schema receipts, focused
regressions and one completed full-suite JUnit/log/exit-0 receipt.
A running command, a prior component PASS or a health response from a stale
process is not proof of the current release. Run FirstByte and BacLOUD
sequentially, not as concurrent canaries. Keep operator audit output outside Git.

Inputs provided by the deployment owner (not by this integration task):
FINAL_SHA, approved immutable source tree/wheel, existing configuration,
MAIN (working SQLite), AUTH (protected auth SQLite), OUTPUT (output cache),
authorized service account, protected backup directory, old release pointer,
and existing verifier-key + external identity anchor locations.
Resolve actual paths from the existing host configuration; never substitute
defaults or assume that the two hosts share keys, identities or databases.

## 1. Offline source/process/identity preflight

1. Require git ls-remote origin on the candidate branch to equal FINAL_SHA
   exactly; inspect git show -s and manifest parent/component ancestry.
   Verify that the audited wheel or build copy corresponds to that SHA.
2. Read current /opt/terminal-mcp/current/RELEASE_META.json, immutable
   release directory/pointer, systemctl MainPID, PID command line,
   PID executable and process start time. Capture sanitized outputs, SHA
   comparison and UTC. An existing process cannot be assumed to be the
   proposed build just because the repo checkout changed.
3. Inventory the EXISTING auth.sqlite3 and any SQLite WAL/SHM, existing
   Access verifier key, auth.sqlite3.identity-anchor, runtime SQLite/output,
   Fleet stores, environment/config and signing configuration as one recovery
   unit. Check permissions and file identities without printing any keys,
   secret hashes, OAuth tokens or personal data. Freeze refresh/access
   mutation window before migration if the old deployment has legacy tables.
4. Confirm working/auth SQLite are physically different files (including
   hardlinks); inspect an offline consistent SQLite snapshot using the
   candidate tool, before initializing a runtime process:

   ~~~bash
   "$PY" -m terminal_mcp.auth.identity_migration inspect \
     --main "$MAIN" --auth "$AUTH"
   ~~~

   Record inspect state, table counts, schema versions, migration marker
   and anchor continuity. Valid existing installations must resolve to
   isolated, or a reviewed legacy-present state eligible for the controlled
   offline forward procedure. missing-protected-auth,
   untrusted-protected-auth, conflicting-identity-sources, malformed/missing
   anchor or key mismatch are HARD STOP. Never bootstrap a replacement
   identity over historical data.

5. If forward migration is actually required, stop ALL writers and confirm
   PID zero; take consistent SQLite snapshots including WAL sidecars and
   protected configuration/key/anchor. Use docs/oauth-identity-isolation-runbook.md
   verbatim with an immutable backup directory and --service-stopped.
   Re-run inspect and SQLite quick_check/foreign_key_check, preserve
   OAuth client IDs, redirect URIs, refresh/revocation state, paired devices,
   audit AUTOINCREMENT high-water and existing grants. Do not rotate any
   credential or broaden any scope. Keep backups protected off the checkout.
6. Test restore feasibility before applying a canary change: snapshot
   timestamp, checksum inventory, protected identity triple, old binary
   compatibility and an available reverse migration path if needed.

## 2. Canary cutover (separate approved operator action)

For one host only, stage the *approved* package from FINAL_SHA, keep previous
installed release intact, apply the operator's existing rollout mechanism
in QA/canary channel (no Canonical promotion), and perform a supervised
restart. Verify the new PID differs from the old PID and started after
the cutover. Check all of:

- systemctl service state and MainPID/start timestamp.
- /proc/$PID/exe and /proc/$PID/cmdline against the deployed immutable
  installation path; confirm process's imported package/build matches
  the staged release, not just the source checkout.
- /opt/terminal-mcp/current/RELEASE_META.json source SHA and release
  pointer match FINAL_SHA, and no stale gunicorn/uvicorn workers serve traffic.
- Local GET /health/live returns HTTP 200, ok=true, version and release;
  the release ID corresponds to staged RELEASE_META.json. This endpoint
  reports the stamped release string, not the complete Git SHA by itself.
- Metrics/trace and standard logs contain startup after the expected PID,
  with no unexpectedly reinitialized auth/anchor identity, migration
  errors, SQLite writer conflicts or Access authorization failures.

The operator may capture read-only examples (adjust service name/port to
the actual unit and loopback; do not paste raw environment secrets):

~~~bash
systemctl show terminal-mcp.service -p MainPID -p ActiveState
PID="$(systemctl show terminal-mcp.service -p MainPID --value)"
test "$PID" -gt 0
readlink -f "/proc/$PID/exe"
tr '\0' ' ' < "/proc/$PID/cmdline"
curl --fail --silent --show-error http://127.0.0.1:8080/health/live
~~~

These commands are documentation for future authorized deployment. TMCP-07
does not run them on either live host.

## 3. Functional canary matrix and acceptance

Record UTC, host, exact PID, release SHA, actor, expected/observed behavior,
sanitized traces, command log, exit status, independent reviewer and
rollback readiness for every item:

1. Existing ChatGPT OAuth client IDs and redirect registrations work
   with their *existing* client secrets; refresh and scope continuity,
   token revocation, and prior console/Android pairings are preserved.
   Re-check anchor nonce proof/key binding and no new auth identity.
2. Access Events: valid signed challenge and subscription complete only
   for currently active correct principal, required scope, issuer/slot/agent
   binding. Revoke a disposable existing test grant during callback to
   prove fail-closed/no stored subscription; check delivery-side revocation.
   Real dot signed callback remains NOT TESTED by isolated QA and requires
   the live canary gate. Never test using production-wide revocation.
3. LIVE021: capture one committed command outcome and two correlated
   attempt trace records; committed outcome is never lost or falsely
   described as noncommitted after transport errors. Avoid replaying
   consequential commands without operator approval.
4. MESSAGE: sender receipts accurately distinguish queued/delivered/read/
   replied observations; verify recipient direction and local incoming
   history without fabricated acknowledgements. Confirm payload remains
   local to original host, uses existing 24-hour retention, and does not
   silently relay across hosts.
5. TASKLIST: paginate history while concurrent state events occur; cursor
   respects original stream snapshot without duplicates or skips. Confirm
   state revision changes leave content revision unchanged when only
   state is mutated; content updates change only their documented domain.
6. Compact role/access catalogs (if accepted into FINAL_SHA): verify
   advertised schema matches final runtime MessageRecord.deliveries,
   validation rejects malformed payloads with documented errors, catalog
   checksum matches the deterministic final build, and no unauthorized
   capability appears.
7. Monitor event-loop, SQLite busy/lock, callback rejection counts,
   duplicate deliveries, orphaned subscriptions, uptime and error budgets
   over an agreed bounded observation window before proceeding to BacLOUD.

## 4. Stop/rollback conditions and recoverability

STOP and isolate the canary if the process/commit identity differs, an
existing OAuth client fails due to a changed identity, auth DB or anchor
mismatch, new implicit credential/grant appears, callback revocation is
bypassed, 24-hour local retention or revision separation breaks,
schema artifacts drift, or health degrades. Preserve logs before rollback.

1. Stop ALL writers and take a fresh snapshot of the CURRENT main/auth
   databases, WAL/SHM, key and anchor before moving any release pointer.
   Keep current auth as the authority for tokens/refreshes/revocations
   created during the canary.
2. For an old/new binary sharing the already migrated protected-auth
   layout, revert only the immutable code release pointer after confirming
   schema compatibility. Never restore an older auth snapshot over
   current credentials.
3. If rollback crosses the identity-store migration boundary, follow
   docs/oauth-identity-isolation-runbook.md reverse procedure with a
   separately prepared old-compatible working copy whose legacy identity
   target is empty. Reverse from CURRENT protected auth into that copy
   while stopped, inspect/reconcile revocation and audit sequence, then
   start the old binary. Mixed writers/identities are forbidden.
4. If only working-data reset must be undone while staying on the new
   binary, restore quarantined main/output SQLite and sidecars; retain
   CURRENT auth, verifier key and external anchor. Never substitute a
   different host's auth DB/key/anchor.
5. After rollback, verify old expected PID/release, health, old client
   continuity, token revocation and device pairings; document incident,
   elapsed window, snapshot checksums, approval and follow-up owner.

After FirstByte is accepted, repeat an independent preflight and the same
checks on BacLOUD using BacLOUD's own old release, config, auth/key/anchor
and backups. No automatic cross-host data migration is approved.
