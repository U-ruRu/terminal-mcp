# Architecture

## Layers

`terminal_mcp.core` owns command orchestration and Agent Session semantics. `terminal_mcp.storage` owns SQLite persistence. MCP and HTTP are thin transports over the same `TerminalService`. Terminal execution remains isolated in the terminal adapter.

## Agent coordination

`AgentCoordinator` разделяет три временных понятия:

- Agent Session TTL — 300 секунд; любой вызов с живым `agent_id` продлевает его и одновременно продлевает active-awareness visibility;
- task lease для `run` — 180 секунд; его обновляют только `agent_start` и `agent_task`.

Отдельного active-awareness window нет. Lifecycle-сигнал `finished` показывается ещё 180 секунд.

`agent_start` хранит полный внутренний `agent_id`, но публичные ответы после регистрации используют только NATO-имя без suffix. `agent_task(agent_id, intent)` обновляет только короткий `intent` (до 160 символов) и task lease; исходный `work_scope` сессии сохраняется.

`run` и `read` не возвращают раздутое дерево объектов для фоновой координации. Поле `active_agents` — массив готовых строк вида:

```text
23:32:38 November 10de68b3 — current intent
23:31:02 India started — current intent
23:34:15 Juliett finished — current intent
```

Активная строка содержит последнюю команду агента. Lifecycle `started`/`finished` хранится как короткий сигнал на 180 секунд. Полная команда и внутренний suffix в awareness не попадают.

Global terminal output использует тот же компактный принцип: `HH:MM:SS name cmd_hash text`; scoped output — `HH:MM:SS text`.

`AgentStore` и SQLite сохраняют структурированные данные: sessions, task transitions, activity audit, command attribution и command start/finish timestamps. Компактность является transport/presentation contract, а не потерей внутренней структуры.

Work scopes остаются cooperative metadata. Exact scopes и parent/child directory scopes пересекаются; coordinator сообщает overlap, но не сериализует исполнение.

## Lifecycle

Session-bound coordination calls validate the session, reject stale IDs with `registration_required`, refresh `last_activity_at`, and record audit events. `run` additionally requires a task event no older than 180 seconds and returns `task_context_expired` when the lease is stale. `health`, global `read`, `recovery` and `cancel` remain available without an Agent Session so diagnostics and emergency control cannot be locked out by orchestration metadata. Commands created without an agent are persisted with `agent_id=anonymous`. Sessions become stale after 300 seconds of inactivity; explicit `agent_finish` is optional. Restart does not erase sessions because lifecycle state is persisted in SQLite.
