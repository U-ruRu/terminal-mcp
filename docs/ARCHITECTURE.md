# Architecture

## Layers

`terminal_mcp.core` owns command orchestration and Agent Session semantics. `terminal_mcp.storage` owns SQLite persistence. MCP and HTTP are thin transports over the same `TerminalService`. Terminal execution remains isolated in the terminal adapter.

## Agent coordination

`AgentCoordinator` manages 300-second sessions identified by compact NATO call signs and a separate 180-second task lease used only by `run`. `agent_start` and `agent_task` create or refresh the lease from persisted `agent_task_events`; no schema migration is required. `AgentStore` persists sessions, task transitions, activity audit and recent-command lookup. SQLite schema version 1 contains `agent_sessions`, `agent_task_events`, `agent_activity_events` and `command_agent_attribution` plus lookup indexes.

Command creation and attribution share one SQLite transaction. Attribution stores only `agent_id`, command hash, type, timestamp and a whitespace-normalized 100-character preview. Full commands remain in `commands`. Global read resolves ownership in one batch query.

Work scopes are cooperative metadata. Exact scopes and parent/child directory scopes overlap. The coordinator reports overlaps without serializing execution. This model is the extension point for future coarse-grained leases.

## Compactness and observability

Standard overview returns self, up to 8 active peers, up to 3 recent commands per peer and an overflow count. Prometheus metrics expose aggregate active/session/task/overlap/command counts without agent IDs as labels, keeping cardinality bounded. Agent-specific detail remains in SQLite and structured logs.

## Lifecycle

Session-bound coordination calls validate the session, reject stale IDs with `registration_required`, refresh `last_activity_at`, and record audit events. `run` additionally requires a task event no older than 180 seconds and returns `task_context_expired` when the lease is stale. `health`, global `read`, `recovery` and `cancel` remain available without an Agent Session so diagnostics and emergency control cannot be locked out by orchestration metadata. Commands created without an agent are persisted with `agent_id=anonymous`. Sessions become stale after 300 seconds of inactivity; explicit `agent_finish` is optional. Restart does not erase sessions because lifecycle state is persisted in SQLite.
