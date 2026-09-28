# Fleet resource bounds

The browser Fleet runtime keeps each Terminal MCP profile independent, but applies shared
resource limits so one unavailable server or a larger fleet cannot create reconnect or
battery churn.

- Startup fan-out is capped at 4 actors. A deterministic 10-server fixture verifies the
  peak remains 4 while a failed server does not block the other profiles.
- Every fleet HTTP request, including refresh-token restore and Console snapshot/ticket
  calls, is aborted after 10 seconds by default.
- Auth and WebSocket reconnects use bounded exponential backoff with jitter; the actor
  ceiling remains 15 seconds between attempts.
- A hidden browser tab gets a 30-second grace period, then all fleet actors are stopped.
  Returning to the foreground restarts them through the same bounded startup queue.
- Realtime events are applied synchronously and are not buffered in an application
  queue. Snapshot convergence permits one refresh in flight plus one coalesced follow-up;
  a burst of 20 events is covered by three total snapshot calls including the initial
  snapshot.
- Removing a profile unsubscribes its actor before stopping it. A removed actor can no
  longer publish into fleet snapshots and its realtime socket/timers are torn down.

These bounds are defaults and are injectable in tests so failure, timeout and visibility
behavior is deterministic.
