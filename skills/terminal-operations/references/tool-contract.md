# Контракт терминального инструмента

## Интерфейс

Terminal MCP 0.9 предоставляет десять методов: `agent_start`, `coordinate`, `message`, `agents`, `agent_finish`, `health`, `run`, `read`, `cancel`, `recovery`. MCP и REST Actions используют общий service layer и одинаковую доменную семантику.

## Agent Session

По умолчанию idle TTL равен 300 секундам, intent lease — 180 секундам, абсолютная длительность регистрации — 1500 секундам. Non-blocking warning начинается после 1200 секунд. На 1380-й секунде появляется blocking session ALERT; после reply он снимается и при продолжающейся сессии может появиться снова через 60 секунд. Пороги, repeat interval, флаг включения и текст ALERT задаются конфигурацией сервера.

Каждый agent-bound ответ может содержать `session_status`, `session_started_at`, `session_age_seconds`, `session_remaining_seconds`, `session_warning`, `task_age_seconds`, `max_task_age_seconds`, `preferred_queue_id` и coordination obligations.

Статусы агента: `started`, `active`, `idle`, `finished`, `forced`. `finished` означает явный `agent_finish`; `forced` имеет persisted reason.

## `agent_start(...)`

Новая регистрация требует `task_summary`, `intent` и `details`. Она единственный раз возвращает полный credential-like `agent_id`. `work_scope` остаётся optional cooperative metadata. Вызов с существующим полным `agent_id` обновляет план текущей живой сессии.

## `coordinate(agent_id, step?, intent?, show_details=false)`

Читает текущий step/intent/detail. `step + intent` фиксирует новый intent event и обновляет intent lease. `show_details=true` сохраняется для compact peer coordination; историческое наблюдение выполняется через `agents`.

## `message(...)`

Send mode:

`message(agent_id, text, target?, require_reply=false, alert=false)`

Без target создаётся broadcast по snapshot текущих active peers. `alert=true` автоматически требует reply.

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

Overview показывает status, относительную последнюю активность, `last_activity_tool`, preferred queue, последнюю команду и coordination counters. При `target` session также содержит `message_journal` с hash, sender, текстом и persisted receipt state `delivered|seen|read|replied` за выбранное history window.

## `run(agent_id, cmd, queue_id?)`

Сохраняет команду в SQLite и помещает её в numbered FIFO lane. Первый вызов без `queue_id` выбирает least-loaded lane и сохраняет affinity. Следующие вызовы используют preferred queue. Явный `queue_id` меняет affinity.

Успешный ответ содержит `cmd_hash`, `queue_id` и `queue_position` (position может стать `null`, если worker уже atomically claimed команду).

`run` требует live session, fresh intent, read acknowledgement всех unread messages и replies для обязательных сообщений.

## `read(agent_id?, cmd_hash?, lines_count=500, offset?)`

`agent_id` и `cmd_hash` независимы. `cmd_hash` выбирает scoped command output; отсутствие hash читает global stream. `agent_id` добавляет session/message context. Обычное unread message отображается и не блокирует read. `ALERT` блокирует read до reply.

Scoped response содержит `status`, `exit_code`, `queue_id`, `queue_position`, line counters, `output_truncated`, `output_retained`, `output_pruned_at`, `output_bytes` и `error`. Persisted output ограничен 4 MiB на логическую строку и 8 MiB на команду. Global lines имеют форму:

`HH:MM:SS <public-name|anonymous> <cmd_hash> qN <output>`

Recovery-команды без numbered lane могут не иметь `qN`.

## `cancel(cmd_hash, agent_id?)`

Работает для команды любой очереди и любого агента. Queued cancellation atomically выполняет `queued -> cancelled`. Running cancellation останавливает process group и фиксирует `running -> cancelled`. Итог проверяется через `read`.

## `recovery(cmd, agent_id?)`

Persisted emergency execution вне numbered queues. Выполняется независимо от занятых lanes и остаётся доступным при ALERT. Вывод сохраняется в общем bounded output-cache и читается через `read`.

## `health(agent_id?)`

Anonymous health показывает приложение, storage и terminal scheduler. `terminal.scheduler` для 0.9 — `numbered-fifo`; `terminal.queues` содержит состояние каждой lane, `parallelism` — число workers, `worker_health` — их состояние. `terminal.output_cache` содержит logical/allocated bytes, target/max, строки, retained/truncated commands и `last_prune_at`. С `agent_id` ответ также содержит session timing, preferred queue и message obligations.

## Command status

`queued`, `running`, `completed`, `failed`, `cancelled`, `not_found`.

Queue lifecycle использует guarded transitions: `queued -> running|cancelled`, затем `running -> completed|failed|cancelled`.

## REST Actions

- `POST /actions/agent/start`
- `POST /actions/coordinate`
- `POST /actions/message`
- `POST /actions/agents`
- `POST /actions/agent/finish`
- `POST /actions/run`
- `POST /actions/read`
- `POST /actions/cancel`
- `POST /actions/recovery`
- `GET /actions/health`
