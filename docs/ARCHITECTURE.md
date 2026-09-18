# Architecture

## Layers

`terminal_mcp.core` owns command orchestration and Agent Session semantics. `terminal_mcp.storage` owns SQLite persistence. MCP and HTTP are thin transports over the same `TerminalService`. Terminal execution remains isolated in the terminal adapter.

## Agent coordination

`AgentCoordinator` manages 300-second sessions identified by compact NATO call signs. `AgentStore` persists sessions, task transitions, activity audit and recent-command lookup. SQLite schema version 1 adds `agent_sessions`, `agent_task_events`, `agent_activity_events` and `command_agent_attribution` plus lookup indexes.

Command creation and attribution share one SQLite transaction. Attribution stores only `agent_id`, command hash, type, timestamp and a whitespace-normalized 100-character preview. Full commands remain in `commands`. Global read resolves ownership in one batch query.

Work scopes are cooperative metadata. Exact scopes and parent/child directory scopes overlap. The coordinator reports overlaps without serializing execution. This model is the extension point for future coarse-grained leases.

## Compactness and observability

Standard overview returns self, up to 8 active peers, up to 3 recent commands per peer and an overflow count. Prometheus metrics expose aggregate active/session/task/overlap/command counts without agent IDs as labels, keeping cardinality bounded. Agent-specific detail remains in SQLite and structured logs.

## Lifecycle

A working tool call validates the session, rejects stale IDs with `registration_required`, refreshes `last_activity_at`, and records an audit event. Sessions become stale after 300 seconds of inactivity; explicit `agent_finish` is optional. Restart does not erase sessions because lifecycle state is persisted in SQLite.
