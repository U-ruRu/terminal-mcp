# Architecture

## Layers

`terminal_mcp.core` owns command orchestration and Agent Session semantics. `terminal_mcp.storage` owns SQLite persistence. MCP and HTTP are thin transports over the same `TerminalService`. Terminal execution remains isolated in the terminal adapter.

## Agent coordination

`AgentCoordinator` разделяет session liveness, execution intent и межагентную коммуникацию.

- Session TTL — 300 секунд; любой вызов с живым `agent_id` продлевает его и active-awareness visibility.
- Task lease для новой `run` — 180 секунд; его обновляют новая регистрация, `agent_start(agent_id=...)` и `coordinate(step + intent)`.
- `finished` показывается ещё 180 секунд.

Новая регистрация хранит обязательный `details`-план, `current_step` и текущий `intent`. `work_scope` остаётся optional cooperative metadata, а не основным coordination primitive. Повторный `agent_start` с полным ID редактирует план без новой identity.

Полный `agent_id` публикуется только при новой регистрации. Последующие ответы и message target используют короткое NATO-имя.

`coordinate` читает текущие step/intent/detail, переключает step и при `step + intent` обновляет task lease. `show_details=true` возвращает планы других active agents.

`message` хранится как mailbox с recipient snapshot и per-recipient receipt. Direct target задаётся коротким именем; отсутствие target означает broadcast текущим active peers. Pending message попадает во все agent-bound operational responses и блокирует только постановку новой `run`.

`active_agents` остаётся compact `string[]`:

```text
23:32:38 November 10de68b3 — current intent
23:31:02 India started — current intent
23:34:15 Juliett finished — current intent
```

Pending message:

```text
23:35:10 a1b2c3d4 India → you: coordination text | ack: message(a1b2c3d4)
```

`AgentStore` и SQLite сохраняют sessions, `details/current_step`, task transitions, optional scopes, message recipients/receipts, activity audit и command attribution. Schema v3 мигрируется без сброса существующей базы; legacy sessions без details переводятся в expired и должны зарегистрироваться заново с планом.

## Lifecycle

Session-bound coordination calls validate the session, reject stale IDs with `registration_required`, refresh `last_activity_at`, and record audit events. `run` additionally requires a task event no older than 180 seconds and no unread coordination message; stale task context returns `task_context_expired`, unread mail returns `coordination_message_pending`. `health`, global `read`, `recovery` and `cancel` remain available without an Agent Session so diagnostics and emergency control cannot be locked out by orchestration metadata. Commands created without an agent are persisted with `agent_id=anonymous`. Sessions become stale after 300 seconds of inactivity; explicit `agent_finish` is optional. Restart does not erase sessions because lifecycle state is persisted in SQLite.
