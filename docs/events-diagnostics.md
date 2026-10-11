# Events diagnostics

Detailed Events ingress, JSON-RPC stage and callback-verification diagnostics are **off by default**.
They use the existing `TERMINAL_MCP_DETAILED_LOGGING` runtime setting, rather than a second switch.

The runtime file defaults to `/etc/terminal-mcp/runtime.env` and can be changed by
`TERMINAL_MCP_RUNTIME_CONFIG_PATH`. Add or update this line in that file to enable diagnostics:

```dotenv
TERMINAL_MCP_DETAILED_LOGGING=true
```

Restore the production default after the investigation:

```dotenv
TERMINAL_MCP_DETAILED_LOGGING=false
```

The runtime watcher reloads changes without restarting the service. An absent setting or an absent
runtime file defaults to false. Invalid runtime configuration retains the last valid settings;
use a valid `false` value to turn diagnostics off. This flag also controls existing detailed service
logging. Normal log-level filtering still applies.

When disabled, the outer Events ingress probe delegates the original ASGI receive/send functions
without buffering or parsing request bodies. Callback response classification is skipped unless an
explicit diagnostic observer is supplied. Callback verification, response-size limits, signatures,
authorization, durable source/ledger, deduplication, dispatch and retry behavior are unchanged.

When enabled, `tmcp_events_diag` records allowlisted protocol stages and server-generated correlation
IDs. The committed `events/unsubscribe` receipt uses `stage=persistence`, `code=unsubscribed` and is
emitted only after removal commits. `tmcp_events_callback` contains bounded response categories,
status, timing, sizes and header-presence booleans. Neither mode logs callback URLs, request/response
bodies, challenges, payload data, authorization headers, signing keys or arbitrary exception text.

Minimal `tmcp_events_error` warning records remain available with detailed logging disabled for
worker/internal failures, callback-verification failures and failed delivery attempts. These include
only fixed failure categories, a server correlation ID and a bounded HTTP status when available.
They are rate-limited per failure category to one record per minute per adapter. Durable delivery
attempt/status records remain the authoritative evidence for individual retries.

Metrics, health diagnostics and SQLite operational diagnostics keep their existing controls. No
logs or stored events are purged by enabling or disabling this flag.
