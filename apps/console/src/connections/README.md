# Local connection registry

BrowserConnectionRegistry is the browser-local source of truth for Terminal MCP server
profiles. The registry is never synchronized through a central service.

Each profile stores a stable local instanceId, canonical server origin, user-editable
displayName, safe device metadata and a credentialRef. Refresh credentials are stored
under a separate per-profile vault key. Pairing secrets and access tokens are never written
to the registry or credential vault.

The first load migrates the M1 singleton connection record into the registry atomically:
the scoped credential is written, the registry is committed, then the legacy singleton
record is removed. If the registry commit fails, the legacy record remains available.

Adding a CLI pairing link validates /connect#secret, rejects an already-registered
canonical origin before network exchange, exchanges the one-time secret, then creates the
profile. Rename changes only local presentation metadata. Disconnect/remove deletes the
profile and its scoped credential without affecting other servers.
