# MCP Events contract: local agent presence

Status: specification only; no production implementation. Base: Canonical 8ea0a40de0e059ae16640adf2f0163f03d20a850.

## Scope

Expose three business events, scoped to presence or a limited Access session on this node:

| Event | Contract |
| --- | --- |
| agent.attached | Once when an agent transitions from absent/detached to active local presence on this node. An Access grant or an agent.started event on another node alone is not an attach. |
| agent.inactive | Once after strictly more than 180 seconds without real activity by that target on this node. Real target activity rearms the one-shot. Observer reads and another agent's heartbeat do not count. |
| session.ended | Once when the existing limited Access WorkSession is manually ended or reaches its existing hard expiry, for each affected local presence. |

For foreign agents, the inspected source has a real local attach path: core/agents.py::_attach_foreign validates the foreign active session and then creates a local agent_sessions row with source_instance_id, or reactivates that local row after idle timeout. storage/events.py journals inserts as agent.started. http/console_events.py::_project_activity_event changes that locally read journal entry to agent.attached only when the stored source_instance_id is foreign to this instance.

That console mapping is a read-side projection, not a webhook producer. The implementation must emit at the successful local presence materialization/reactivation boundary, not by forwarding the remote agent.started event or reacting to a remote Access grant. Add a test proving that the local row exists before delivery and that duplicate/replayed input emits once. The local-start path currently remains agent.started in the console projection; confirm whether local starts are in the agent.attached product meaning before including them.

For inactivity, core/agents.py updates last_activity_at on real target actions; observer paths use touch=False. The implementation must use the target's local activity clock and must not let observation refresh it.

Do not change the existing limited-session default of 1,380 seconds (23 minutes), add auto-renewal or a new session manager, or introduce inter-server synchronization. Do not send periodic inactivity reminders. Reuse the existing local presence/session projection and current Mesh delivery path only.

## User-visible payload

The event name is in the protocol envelope. The data object contains only the agent display name and server name; task is optional only when a short task value is already present in the source event without lookup:

    {"agent_name":"Agent-1","server":"node-1","task":"optional existing summary"}

Omit task when unavailable. Do not include agent, session, or task IDs, intent, end reason, rich context, or log/comment bodies. Keep eventId stable across retries.

## MCP Events wire contract and compatibility

Use the official ChatGPT MCP Events profile: MCP 2.0 protocol 2026-07-28, webhook delivery, and server/discover advertising capabilities.events. Implement server/discover, events/list, events/subscribe, and events/unsubscribe as JSON-RPC methods on the same authenticated endpoint as existing tools.

Current FastMCP endpoints are /mcp, /terminal-mcp/executor/v1/mcp, /terminal-mcp/coordinator/v1/mcp, and optional /terminal-mcp/access/v1/mcp. Existing FastMCP tool registration is in mcp/server.py and mcp/roles.py; MCP protection and OAuth metadata are in auth/middleware.py and auth/routes.py. This Canonical revision has no Events handlers or server/discover implementation. Add an adapter at the route boundary while preserving current initialize, tools/list, tools/call, endpoint paths, and OAuth behavior.

Discovery advertises supportedVersions ["2026-07-28"] and capabilities {"tools":{},"events":{}}. events/list returns event definitions with name, delivery ["webhook"], inputSchema, and payloadSchema.

Subscription request:

    {"jsonrpc":"2.0","id":2,"method":"events/subscribe","params":{
      "name":"agent.inactive","arguments":{},
      "delivery":{"mode":"webhook","url":"https://receiver.example.test/mcp-events","secret":"whsec_<base64-signing-key>"},
      "cursor":null
    }}

Successful response:

    {"jsonrpc":"2.0","id":2,"result":{
      "id":"sub_example","refreshBefore":"2026-10-11T00:00:00Z","cursor":null,"truncated":false
    }}

Unsubscribe uses the same name, arguments, and callback URL in delivery, and returns result {}. Delivery is one signed HTTPS POST per event:

    {"eventId":"evt_example","name":"agent.inactive","timestamp":"2026-10-10T12:00:00Z","data":{"agent_name":"Agent-1","server":"node-1"},"cursor":null}

Use Standard Webhooks headers: webhook-id equals eventId, webhook-timestamp, webhook-signature, and X-MCP-Subscription-Id. Follow the official callback challenge, authorization, persistent subscription, callback URL and SSRF validation, 256 KiB limit, bounded retry, and no-retry rules for 410/413. These are requirements for a future implementation, not current behavior.

## Acceptance matrix

| Case | Expected |
| --- | --- |
| Local foreign-session materialization / remote start or grant only | One local agent.attached after committed presence / no event |
| Inactivity at 180 s / after 180 s | No event / one agent.inactive |
| Observer read or another agent heartbeat | Does not reset target clock |
| Real target action after inactivity | Rearms one later inactivity event; no periodic repeats |
| Manual WorkSession end / existing hard expiry | One session.ended per affected local presence |
| Duplicate source record or webhook retry | No duplicate business event; stable eventId across retries |
| Existing MCP tools and OAuth client | Existing initialize/list/call behavior and auth remain available on the same routes |

## Delivery and verification

1. Add the Events protocol adapter and catalog without replacing FastMCP or OAuth.
2. Connect the three event producers to verified local presence/session boundaries with internal deduplication.
3. Run the matrix, then verify callback challenge, subscription persistence/refresh, signed HTTPS delivery, and unsubscribe against a configured receiver before enabling subscriptions.

Open verification: test that manual end and hard expiry reach every affected local presence through the existing local projection. The foreign attach boundary is identified in source above; tests must establish event timing and deduplication for both create and idle-timeout reactivation. Live OAuth/plugin callbacks and deployed-node delivery are not verified.

Official reference: [OpenAI MCP Events documentation](https://developers.openai.com/plugins/build/mcp-events).
