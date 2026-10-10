# Secondary runtime drift and stability QA — October 10, 2026

Independent, non-mutating production QA by Juliett-23 via Main -> SSH Secondary.
No shared branch, database, service or production Mesh routing was changed.

## Versions and compatibility

- Secondary runtime: Terminal MCP 0.13.1, installed Oct 7; primary SQLite user_version 20.
- Canonical branch: e0f2de4 at audit time; storage schema target 23.
- Firstbyte and BacLOUD: 0.20.7-e0f2de4, storage schema 23.
- Secondary has old terminal_mcp.mcp.output_contracts.SessionSummary:
  mode and authority_node_id are mandatory. Managed coordinator sessions
  may omit these fields or supply null, so old observe(sessions) crashes.
- Canonical already treats these fields as optional. Both missing-field
  and explicit-null model validation fail on installed Secondary 0.13.1,
  but pass on canonical. Three unit regressions in associated test file.
- New Secondary Executor managed session start yielded internal_error
  for this caller; this is an additional defect, not proven to have the
  same cause as the SessionSummary model. Coordinator/Executor reads
  sometimes returned identity_not_bound.

## Storage QA

- Secondary live: 88 MiB SQLite, quick_check(1)=ok, zero FK violations.
- Auth database schema=3; main database schema=20; current code target=23.
- Consistent SQLite online backup into private temporary QA directory,
  then canonical SqliteRepository.initialize(preserve_active_commands=True)
  on that snapshot ONLY: schema 20 -> 23 PASS, quick_check=ok,
  foreign key violations=0; about 2 seconds including backup.
  Temporary private database files were removed afterward.
- At 10:47 UTC, Secondary 335 instance_events in 10 min (not a
  no-op event storm). Firstbyte 51 and BacLOUD 59 events in 5 min.
  Live Firstbyte/BacLOUD quick_check=ok and FK violations=0.

## Transport and app QA

- Production authenticated Access Mesh snapshot(limit=1) on HTTPS:
  Firstbyte->BacLOUD 3/3 PASS median 63ms; BacLOUD->Firstbyte
  3/3 PASS median 69ms. No ReadTimeout in this narrow sample.
- Secondary to public Cloudflare health endpoints: Firstbyte 3/3
  HTTP 200 (0.19-0.43 seconds), BacLOUD 3/3 HTTP 200 (0.06s).
- Both new node coordinator health endpoints report 0.20.7, healthy
  storage, workflow, Access Mesh and messaging.
- Production WireGuard peer overrides absent; dedicated new Mesh WG
  systemd services disabled/inactive on both. Leave active Mesh
  routing on HTTPS until the Fleet topology owner completes onboarding.
- Firstbyte has two failed ephemeral archc activation services dated
  October 6: api_paths_not_provisioned and commands_not_drained:1.
  These are historical, not current failures. No reset was attempted.

## Role boundary and upgrade gate

Current coordinator Lima/Mike handles M4 forwarded Fleet detach/rejoin.
Lead developer Kilo handles P0 durable command run/cancel receipts.
This QA did not modify their code, active worktrees or databases.

Before upgrading protected Secondary production, coordinator should:
1. Announce maintenance to active work sessions, reconcile in-flight
   commands, and take consistent DB snapshots and release metadata.
2. Prepare signed canonical artifact and test correct version bump from
   Secondary's own installed baseline, not from Firstbyte.
3. Use the standard installer and verify schema23, auth, Coordinator
   session start/attach, observe(sessions), Task commands, and Fleet.
4. Preserve rollback and monitor immediate API, SQLite and network
   reliability. Long soak tests can follow on the live system.

Do not perform a blind 0.13.1 -> 0.20.7 production deployment while
other developers are actively using Secondary without coordinator approval.
