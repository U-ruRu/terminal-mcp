# Browser credential lifecycle

The Console keeps the one-time pairing secret only in `location.hash`. `ConnectionManager`
removes that fragment with `history.replaceState` before cryptography or network I/O starts.
The pairing secret is never written to application state or browser storage.

`BrowserCredentialVault` uses a namespaced `localStorage` record containing only the instance
origin, device/client identifiers, display label, scope, pairing timestamp and refresh token.
The short-lived access token stays in memory. Refresh rotation replaces the stored refresh token.

Transient network/server errors keep refresh material for explicit retry. `invalid_client` means
the paired device was revoked server-side; `invalid_grant` means the refresh credential expired
or is no longer usable. Both terminal states clear local credential material.
`disconnect()` is local-only and clears the vault immediately.

M0 intentionally exposes device revocation only through the server-local
`terminal-mcp devices revoke <device_id>` command. The browser does not invent a remote revoke
endpoint; it detects server-side revocation on the next refresh and presents `revoked`.
