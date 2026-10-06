# Fleet-control raw-writer attribution

This procedure is for a scrubbed test copy only. The tracer refuses targets
outside /tmp or /var/tmp and refuses names other than fleet-control.sqlite3.
Do not point it at live Fleet state or preserved incident evidence.

The 2026-10-06 incident shape is a valid SQLite main database whose first
32 bytes were replaced by a SQLite WAL header while the rest of the main file
remained intact. Normal FleetControlStore WAL concurrency, reopen and checkpoint
stress does not produce that shape. The repository deploy/backup paths also
contain no raw sidecar-to-main copy path.

Linux fanotify can attribute the external file writer by PID, process name and
executable without logging argv/cmdline, where pairing links or credentials
could appear.

Controlled use:

    python scripts/trace_fleet_control_writer.py \
      --sandbox-root /tmp/fleet-writer-case \
      --target /tmp/fleet-writer-case/fleet-control.sqlite3 \
      -- python /tmp/fleet-writer-case/reproducer.py

The JSON result records only the sandbox-relative path, PID, comm, executable,
event mask and first 32 bytes after the event. A transition from sqlite_main to
wal_header proves that a raw filesystem writer changed the main file; it does
not by itself identify the production incident process.

Current evidence therefore supports these boundaries:

- do not attribute the overwrite to SQLite/WAL concurrency without a reproducer;
- do not attribute backup or deployment code without a raw-write reproducer;
- treat a future recurrence as an external-writer attribution event and capture
  PID/executable on an isolated reproduction before changing production code;
- FleetControlStore remains fail-closed when a WAL header appears at the main
  path, so corrupted state is not silently reopened or rewritten.
