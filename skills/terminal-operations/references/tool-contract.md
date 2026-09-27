# Контракт терминального инструмента

## Интерфейс

Terminal MCP 0.9.2 предоставляет двенадцать методов: `agent_start`, `coordinate`, `message`, `agents`, `agent_finish`, `tasks`, `task`, `health`, `run`, `read`, `cancel`, `recovery`. MCP и REST Actions используют общий service layer и одинаковую доменную семантику.

## Agent Session

По умолчанию idle TTL равен 300 секундам, intent lease — 180 секундам, абсолютная длительность регистрации — 1500 секундам. Non-blocking warning начинается после 1200 секунд. На 1380-й секунде появляется blocking session ALERT; после reply он снимается и при продолжающейся сессии может появиться снова через 60 секунд. Пороги, repeat interval, флаг включения и текст ALERT задаются конфигурацией сервера.

Agent-bound operational success по умолчанию возвращает только результат операции. Session/message context добавляется, когда он меняет следующее действие: warning, unread/reply-required message, ALERT, expiry или registration requirement. Полный session/fleet context читается через observation tools.

Статусы агента: `started`, `active`, `idle`, `finished`, `forced`. `finished` означает явный `agent_finish`; `forced` имеет persisted reason.

## `agent_start(...)`

Новая регистрация требует `task_summary`, `intent` и `details`. Она единственный раз возвращает полный credential-like `agent_id`. `work_scope` остаётся optional cooperative metadata. Вызов с существующим полным `agent_id` обновляет план текущей живой сессии.

## `coordinate(agent_id, step?, intent?, show_details=false)`

Читает текущий step/intent/detail. `step + intent` фиксирует новый intent event и обновляет intent lease. `show_details=true` сохраняется для compact peer coordination; историческое наблюдение выполняется через `agents`.

## `message(...)`

Send mode:

`message(agent_id, text, target?, namespace?, task_id?, require_reply=false, alert=false)`

`target` адресует active agent, отсутствие target создаёт broadcast active peers snapshot, а `namespace + task_id` адресуют managed task: сообщение snapshot-доставляется текущим live claimants и одновременно сохраняется в durable task history. Agent target и task target взаимоисключающие. `alert=true` автоматически требует reply.

Read acknowledgement:

`message(agent_id, message_hash)`

Получатель явно подтверждает, что сообщение прочитано. Сам показ сообщения выставляет только `seen`. До acknowledgement обычное сообщение блокирует новую `run` и повторно показывается. Полный текст гарантирован минимум 180 секунд и минимум пять surfaced responses, затем остаётся compact reminder.

Reply:

`message(agent_id, message_hash, text)`

Создаёт связанный ответ исходному отправителю и закрывает reply obligation. `require_reply` блокирует `run` до reply. `ALERT` блокирует normal work surface до reply; `message`, `health`, `cancel`, `recovery` и `agent_finish` сохраняют доступ. Late-session ALERT создаётся системным sender и после снятия повторяется по configured interval, пока та же сессия продолжает использоваться.

Sender inspection через `message(sender_id, message_hash)` возвращает `delivered_to`, `seen_by`, `read_by`, `replied_by`.

## `agents(...)`

`agents()` без параметров — read-only fleet observer, регистрация для просмотра не требуется.

Параметры:

- `agent_id?` — контекст вызывающей Agent Session;
- `target?` — public agent name;
- `show_details` — план, scope и lifecycle details;
- `show_intents` — intent journal;
- `show_commands` — command journal с preview до configured limit;
- `command_hash?` — полная исходная команда и metadata;
- `since_minutes?` — относительное окно истории.

По умолчанию overview компактный: status, activity age, intent/step и managed-task refs. `show_details` раскрывает plan/scope, `show_commands` — command data, `target` — выбранную session и message journal с persisted receipt state `delivered|seen|read|replied`.


## `tasks(namespace?, task_id?, lane?, state?, show_details=false, show_done=false, show_archived=false, limit=50, cursor?)`

Read-only локальный backlog observer. Без selector возвращает compact unfinished tasks, counts/pressure и `recommended`; namespace фильтрует пространство, `namespace + task_id` выбирают одну карточку. `show_details=true` раскрывает description, resource context, dependencies, reviews и recent events. `show_done=true` включает завершённые задачи, `show_archived=true` — архив. `state=archived` выбирает архив напрямую. Cursor используется для истории/больших выборок.

## `task(agent_id, action, namespace, ...)`

Явно изменяет managed task. Базовые actions: `create`, `claim`, `release`, `update`, `checkpoint`, `review`, `state`, `done`, `archive`. `archive` требует свободный текст `note`, переводит task в `archived`, освобождает live claims и сохраняет note/release evidence в durable history. Архив скрывается из обычного backlog; прямой `update/state` в `archived` отклоняется, чтобы audit note был обязательным. Namespace обязателен. Fixed lanes: `implementation`, `review`, `release`, `integration`, `general`; durable states: `ready`, `blocked`, `deferred`, `done`, `archived`; review dimensions: `A`, `C`, `R`. Tool schemas публикуют фиксированные enum для action, lane, state, priority, review dimensions и verdict. Переход в `done` атомарно освобождает все текущие claims и сохраняет release events; последующий явный claim завершённой задачи остаётся разрешённым с warning. Scheduler только рекомендует. Multiple claims разрешены. Dependency, self-review, concurrent claim, stale candidate и unusual transition возвращаются structured warnings и сохраняют наблюдаемость вместо workflow lock. Ad-hoc terminal work не требует managed task.

## `run(agent_id, cmd, queue_id?)`

Сохраняет команду в SQLite и помещает её в numbered FIFO lane. Первый вызов без `queue_id` выбирает least-loaded lane и сохраняет affinity. Следующие вызовы используют preferred queue. Явный `queue_id` меняет affinity.

Успешный ответ содержит `cmd_hash`, `queue_id` и `queue_position` (position может стать `null`, если worker уже atomically claimed команду).

`run` требует live session, fresh intent, read acknowledgement всех unread messages и replies для обязательных сообщений.

## `read(agent_id?, cmd_hash?, lines_count=500, offset?)`

`agent_id` и `cmd_hash` независимы. `cmd_hash` выбирает scoped command output; отсутствие hash читает global stream. Обычный successful read остаётся компактным; agent context добавляется при actionable coordination state. Обычное unread message отображается и не блокирует read. `ALERT` блокирует read до reply.

Scoped response содержит `status`, `exit_code`, `queue_id`, `queue_position`, line counters, `output_truncated`, `output_retained`, `output_pruned_at`, `output_bytes` и `error`. Persisted output ограничен 4 MiB на логическую строку и 8 MiB на команду. Global lines имеют форму:

`HH:MM:SS <public-name|anonymous> <cmd_hash> qN <output>`

Recovery-команды без numbered lane могут не иметь `qN`.

## `cancel(cmd_hash, agent_id?)`

Работает для команды любой очереди и любого агента. Queued cancellation atomically выполняет `queued -> cancelled`. Running cancellation останавливает process group и фиксирует `running -> cancelled`. Итог проверяется через `read`.

## `recovery(cmd, agent_id?)`

Persisted emergency execution вне numbered queues. Выполняется независимо от занятых lanes и остаётся доступным при ALERT. Вывод сохраняется в общем bounded output-cache и читается через `read`.

## `health(agent_id?)`

Anonymous health показывает фактическую application version, storage, terminal scheduler и компактный `workflow` aggregate: counts по state/lane, active/stale claims и reviews без backlog payload. `terminal.scheduler` для 0.9.x — `numbered-fifo`; `terminal.queues` содержит состояние каждой execution lane, `parallelism` — число workers, `worker_health` — их состояние. `terminal.output_cache` содержит logical/allocated bytes, target/max, строки, retained/truncated commands и `last_prune_at`. С `agent_id` actionable session/message context добавляется при необходимости.

## Command status

`queued`, `running`, `completed`, `failed`, `cancelled`, `not_found`.

Queue lifecycle использует guarded transitions: `queued -> running|cancelled`, затем `running -> completed|failed|cancelled`.

## REST Actions

- `POST /actions/agent/start`
- `POST /actions/coordinate`
- `POST /actions/message`
- `POST /actions/agents`
- `POST /actions/agent/finish`
- `POST /actions/tasks`
- `POST /actions/task`
- `POST /actions/run`
- `POST /actions/read`
- `POST /actions/cancel`
- `POST /actions/recovery`
- `GET /actions/health`
