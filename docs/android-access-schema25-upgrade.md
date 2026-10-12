# Android Access schema 25 upgrade

This candidate requires backend management contract version 2 for the accepted
Android 0.2.37 (version code 39) APK. Its Android/frontend source remains unchanged.
The backend SQLite storage revision is 25; installer release classification must
recognize this schema change rather than treating the build as code-only.

## Existing native policy and open windows

Before the first incompatible Access schema change, an existing native defaults
table with no singleton row is snapshotted from the configured effective native
policy. For the verified deployment configuration this is duration 1,200 seconds,
cooldown 60 seconds, warning 120 seconds, draining 30 seconds, and legacy admission
enabled. These are preserved values, not new policy settings. The snapshot is an
explicit server override; it prevents ordinary Fleet defaults from silently
replacing the existing native defaults on startup. Existing persisted defaults
and already-open policy windows are preserved. Fresh native installations retain
the existing inheritance behavior.

If main storage is upgraded while native Access is disabled, a durable marker
retains the need to snapshot configured native defaults when Access next starts.
The configured values must remain the intended pre-upgrade native defaults until
that first Access startup. No migration changes deployment configuration.

## Remaining policy limitation

An explicit reset to Mesh inheritance still uses the existing Fleet-to-native
conversion. With the observed Fleet values it produces duration 82,800 seconds,
cooldown 120 seconds, warning 10,800 seconds, and draining 3,600 seconds. This
mapping is not an approved zero-drain native policy. This change does not alter
that future reset behavior or define a new native policy meaning. Do not present
reset-to-inherit, inherited defaults, or this rollout as delivering a zero-drain
policy. A future native policy design and its explicit approval remain separate.

## Interrupted startup and rollback

The first synchronous Access migration transaction commits its incompatible
columns and schema-25 fence atomically. Interrupted work before that commit rolls
back together. Later startup failure leaves a durable version fence; the schema-24
runtime refuses main-repository startup rather than accepting incompatible tables.
A migration-origin marker preserves older conditional main-storage migrations
until successful initialization.

A release-symlink-only rollback to schema-24 code is not supported after migration.
Recovery requires restoring a verified pre-upgrade database backup with the older
runtime, or using a compatible schema-25 runtime. Preserve coordinated backups and
a recovery plan before any rollout. These source and isolated-test changes do not
perform a production migration or authorize deployment.

## Replication recovery

Exact-slot gap repair beyond the first 50-slot page uses the same canonical wire
serializer as normal snapshot pages. Issuer-local code-generation metadata is
excluded, and optional legacy fields retain the existing compatibility projection.
