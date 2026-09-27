# Контракт терминального инструмента

## Интерфейс

Terminal MCP 0.10.0 предоставляет двенадцать методов: `agent_start`, `coordinate`, `message`, `agents`, `agent_finish`, `tasks`, `task`, `health`, `run`, `read`, `cancel`, `recovery`. MCP и REST Actions используют общий service layer и одинаковую доменную семантику.

## Agent Session

По умолчанию idle TTL равен 300 секундам, task-context TTL — 180 секундам, абсолютная длительность регистрации — 1500 секундам. Канонические поля — `task_context_ttl_seconds` и `task_context_age_seconds`; `task_lease_seconds`, `task_age_seconds` и `max_task_age_seconds` остаются compatibility aliases. Non-blocking warning начинается после 1200 секунд. На 1380-й секунде появляется blocking session ALERT; после reply он снимается и при продолжающейся сессии может появиться снова через 60 секунд. Пороги, repeat interval, флаг включения и текст ALERT задаются конфигурацией сервера.

Agent-bound operational success по умолчанию возвращает только результат операции. Session/message context добавляется, когда он меняет следующее действие: warning, unread/reply-required message, ALERT, expiry или registration requirement. Полный session/fleet context читается через observation tools.

Статусы агента: `started`, `active`, `idle`, `finished`, `forced`. `finished` означает явный `agent_finish`; `forced` имеет persisted reason.

## `agent_start(...)`

Новая регистрация требует `task_summary`, `intent` и `details`. Она единственный раз возвращает полный credential-like `agent_id`. `work_scope` остаётся optional cooperative metadata. Вызов с существующим полным `agent_id` обновляет план текущей живой сессии.

## `coordinate(agent_id, step?, intent?, show_details=false)`

Читает текущий step/intent/detail. `step + intent` фиксирует новый intent event и обновляет task-context freshness. `show_details=true` сохраняется для compact peer coordination; историческое наблюдение выполняется через `agents`.

## `message(...)`

Send mode:

`message(agent_id, text, target?, namespace?, task_id?, require_reply=false, alert=false)`

`target` адресует active agent, отсутствие target или `target="broadcast"` создаёт broadcast active peers snapshot, а `namespace + task_id` адресуют managed task: сообщение snapshot-доставляется текущим live claimants и одновременно сохраняется в durable task history. Agent target и task target взаимоисключающие. `alert=true` автоматически требует reply.

Read acknowledgement:

`message(agent_id, message_hash)`

Получатель явно подтверждает, что сообщение прочитано. Сам показ сообщения выставляет только `seen` и не является acknowledgement. До явного `message(agent_id, message_hash=...)` состояние помечается `ACK REQUIRED`; обычное сообщение блокирует новую `run` и повторно показывается. Полный текст гарантирован минимум 180 секунд и минимум пять surfaced responses, затем остаётся compact reminder.

Reply:

`message(agent_id, message_hash, text)`

Создаёт связанный ответ исходному отправителю и закрывает reply obligation. `require_reply` блокирует `run` до reply. `ALERT` блокирует normal work surface до reply; `message`, `health`, `cancel`, `recovery` и `agent_finish` сохраняют доступ. Late-session ALERT создаётся системным sender и после снятия повторяется по configured interval, пока та же сессия продолжает использоваться.

После normal `agent_finish` exact old `agent_id` в течение 300 секунд может только ACK/reply уже delivered ему message hash; grace не разрешает новые sends или другие agent tools.

Sender inspection через `message(sender_id, message_hash)` возвращает `delivered_to`, `seen_by`, `read_by`, `replied_by` и отдельно `inactive_recipients`. Receipt state не меняется от завершения recipient session. `agent_finish` разрешён при pending communication и может вернуть `pending_communication {unacknowledged, reply_required, alerts}`; post-finish grace действует только для exact old `agent_id` и только для ACK/reply ранее доставленного hash.

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


## `tasks(namespace?, task_id?, lane?, state?, tags?, show_details=false, show_done=false, show_archived=false, limit=50, cursor?)`

Read-only backlog observer. `tags` uses AND semantics; `tag_counts` exposes vocabulary. Default response returns active backlog, `claimable_count`, weighted pressure and recommended task; priority sorts first, equal priority uses oldest `ready_since`. Summary includes `oldest_claimable_ready_since` / age and `missing_dependency_count`. Detailed lookup exposes dependencies, relations, claims, comments/history, archive lifecycle metadata and legacy reviews. `show_archived=true` selects archived lifecycle records; archive is not a workflow state.

## `task(agent_id, action, namespace, ...)`

Workflow states are `ready|blocked|deferred|done`. `done` means goal reached and requires meaningful `result`. Claimed blocked requires `blocker_reason`; release live claim requires `release_reason`, который сохраняется как durable handoff history entry. `action=comment` with `comment_text` appends durable history distinct from mutable description.

Primary claim requires `claim_intent` (max 160); repeating own claim updates intent. `cooperative` управляет concurrent participation, а не visibility. Earliest live claim is `owner`; later cooperative claims are `participants`. Claim view exposes `claimed_at`, `claim_age_seconds`, `claim_intent` and `role`. Owner-only mutations include checkpoint, state/done/blocked, dependencies and ownership-affecting cooperative changes. Participant may comment, run commands using its task_scope and change only safe metadata. `cooperative=true -> false` rejects while multiple live claims exist.

Dependencies are a validated directed graph: self edge and cycle reject before persistence. Missing target is allowed, represented as `missing` and remains blocking. Completion satisfies prerequisite independently of archive visibility: archived done satisfied; archived unfinished blocking. `force=true` + `force_reason` is durable conscious override only for dependency claim gate.

Generic relation mutations are `action=relate` / `unrelate` with `relation_kind`, `related_namespace`, `related_task_id`. Relation view entries expose direction/kind/namespace/task_id/created_at. Review uses relation kind `review_of`; success is ordinary done(result), blocking findings are comment + blocked. Reviewed task receives linked `review_feedback` event with review reference, `outcome=done|blocked`, candidate_ref and result or findings/blocker_reason.

`archive` requires `archive_note`, preserves workflow state, stores `archived_at` / archive evidence, releases live claims and removes task from active backlog/pressure/recommendation. Legacy v8 archived-state rows migrate deterministically into v9 lifecycle representation. Ad-hoc terminal work still requires no managed task.

## `run(agent_id, cmd, task_scope, queue_id?)`

Сохраняет команду в SQLite и помещает её в numbered FIFO lane. Первый вызов без `queue_id` выбирает least-loaded lane и сохраняет affinity. Следующие вызовы используют preferred queue. Явный `queue_id` меняет affinity.

`task_scope` обязателен для каждой normal run до enqueue. Без live claims допустим только `none`; при live claims — `none`, `all` или одна собственная live claimed `namespace/task_id`. Текущие legal values возвращаются в `task_scope_options`. Scope управляет только task command events. Успешный ответ содержит `cmd_hash`, `queue_id` и `queue_position` (position может стать `null`, если worker уже atomically claimed команду).

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

Anonymous health показывает фактическую application version, storage, terminal scheduler и компактный `workflow` aggregate: counts по state/lane, canonical `live_claims` / `unreleased_claims` / `stale_claims` и reviews; legacy `active_claims` — compatibility alias для unreleased rows без backlog payload. `terminal.scheduler` — `numbered-fifo`; `terminal.queues` содержит состояние каждой execution lane, `parallelism` — число workers, `worker_health` — их состояние. `terminal.output_cache` содержит logical/allocated bytes, target/max, строки, retained/truncated commands и `last_prune_at`. С `agent_id` actionable session/message context добавляется при необходимости.

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
