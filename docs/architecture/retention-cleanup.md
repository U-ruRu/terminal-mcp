# Terminal MCP retention and cleanup policy

Retention must bound physical storage while preserving active work, the current/previous recovery set, and enough metadata for operational diagnosis.

- OAuth authorization codes: purge immediately after successful use and whenever expired.
- OAuth refresh tokens: purge immediately after revocation/rotation and whenever expired.
- OAuth clients remain until explicit client/device deletion.
- Output cache: logical pruning must be followed by controlled physical compaction when free SQLite pages materially exceed the configured retained headroom; never compact through an active writer transaction.
- Command bodies: keep full bodies only while their command/output history is retained; older rows keep bounded operational metadata/preview.
- Releases: keep current plus previous usable release.
- Automatic DB/release backups: keep the latest two verified usable backups. Manual/operator-labelled backups are outside automatic deletion.
- Deployment units: repository service/install definitions must have one canonical generated model aligned with `/opt/terminal-mcp/current`.

Cleanup must never delete an active command, current release, previous rollback release, live OAuth credential, or the last two verified automatic backups.
