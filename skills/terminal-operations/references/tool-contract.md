# Контракт терминального инструмента

## Интерфейс

Подключённый terminal MCP предоставляет методы `agent_start`, `coordinate`, `message`, `agents`, `agent_finish`, `health`, `run`, `read`, `cancel` и `recovery`. Agent Session TTL составляет 300 секунд. Task lease для `run` составляет 180 секунд и обновляется registration, `agent_start(agent_id=...)` и `coordinate(step + intent)`.

MCP и REST Actions могут использовать общий service layer и одинаковые response-модели.

## Общие статусы

Статус возвращается методом `read`:

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`
- `not_found`

Поле `error` предназначено для ошибок инструмента и таймаутов. Формат: `<method>.<stage>: <reason>`. Ненулевой exit code shell-команды отражается в `exit_code`, а stderr находится в `lines`.

## `health(agent_id?)`

`agent_id` необязателен. Без него health остаётся anonymous диагностикой. С живым ID вызов продлевает Session TTL и включает pending messages. Возвращает состояние приложения, storage, авторизации, terminal adapter и FIFO-планировщика:

- `ok`, `application`, `storage`, `auth_mode`;
- `terminal.ok`, `user`, `uid`, `gid`, `cwd`, `privilege`, `shell`, `terminal_user`;
- `terminal.scheduler`, `parallelism`, `queue_size`, `running_commands`;
- необязательный `custom_command` с `command`, `lines`, `status`, `exit_code`, `error`, `ok`, `duration_ms`.

Настроенная health-команда выполняется отдельно от пользовательской FIFO-очереди.

## `agent_start(...)`

Новая регистрация требует `task_summary`, `intent` и `details` (1–12 шагов по максимум 160 символов). `work_scope` optional: до четырёх элементов по 80 символов. Новый агент единственный раз получает полный внутренний ID. Существующий `agent_id` превращает вызов в update текущего плана.

## `coordinate(agent_id, step?, intent?, show_details=false)`

Только ID возвращает текущий step/intent/detail. `step` выбирает detail. `step + intent` обновляет current work и task lease. `show_details=true` добавляет compact планы peers. Intent без step недопустим.

## `message(agent_id, text?, target?, message_hash?)`

Send mode использует `text` и optional public-name `target`; без target recipients фиксируются как snapshot всех текущих active peers. Ack mode использует только `message_hash` и возвращает `read_by`.

```text
HH:MM:SS a1b2c3d4 India → you: text | ack: message(a1b2c3d4)
HH:MM:SS e5f6a7b8 India → all: text | ack: message(e5f6a7b8)
```

Unread message блокирует новую `run`, но не `read`, `message`, `coordinate`, `health`, `cancel` или `recovery`.

## `run(agent_id, cmd)`

Сохраняет команду и добавляет её в FIFO. Операция ограничена таймаутом инструмента. При невозможности завершить enqueue созданная запись должна быть удалена и не должна выполниться позднее.

Запрос:

```json
{"cmd":"..."}
```

Успешный ответ:

```json
{"ok":true,"cmd_hash":"1a2b3c4d","error":null}
```

Ответ при ошибке инструмента:

```json
{"ok":false,"cmd_hash":null,"error":"run.enqueue: ..."}
```

`cmd_hash` — восемь lowercase hex-символов.

## `read(agent_id?, cmd_hash?, lines_count=500, offset?)`

Возвращает ограниченное окно строк. Для известного `cmd_hash` используй scoped-чтение.

Для конкретной команды ответ содержит:

- `ok`;
- `lines`;
- `next_offset`;
- `overall_lines_count`;
- `displayed_lines_count`;
- `cmd_hash`;
- `status`;
- `exit_code`;
- `error`.

Положительный offset задаёт позицию от начала. Отрицательный offset задаёт позицию от конца. `next_offset` передаётся в следующий вызов для последовательного чтения новых строк.

Глобальное чтение без `agent_id` и `cmd_hash` предназначено для общего журнала и диагностики потерянного хэша. Команды без agent context отображаются как `anonymous`. Его cursor-семантика определяется контрактом конкретного инструмента.

## `cancel(cmd_hash, agent_id?)`

Отменяет queued или running-команду. Ответ подтверждает принятие операции, а фактический итоговый статус проверяется через `read(cmd_hash)`.

Пример ответа:

```json
{"ok":true,"cmd_hash":"1a2b3c4d","error":null}
```

## `recovery(cmd, agent_id?)`

Создаёт команду с собственным `cmd_hash`, сохраняет её и выполняет немедленно вне FIFO. Клиент ждёт завершения или таймаута инструмента.

Запрос содержит `cmd`. Ответ обычно содержит:

- `ok`;
- `cmd_hash`;
- `lines`;
- `overall_lines_count`;
- `displayed_lines_count`;
- `exit_code`;
- `error`;
- `duration_ms`.

Полный вывод после завершения или таймаута дочитывается через `read(cmd_hash)`.

## REST Actions

Типичный HTTP-контракт:

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

Точные лимиты и дополнительные поля определяются актуальным описанием подключённого инструмента.

## Метаданные безопасности клиента

Клиент должен учитывать read-only и consequential/destructive metadata, опубликованные терминальным инструментом, и сопоставлять их с фактической семантикой операции.

## Семантика вывода

- timestamps обычно выводятся в UTC;
- stdout и stderr сохраняют порядок, установленный реализацией;
- structured MCP tools могут возвращать типизированный объект в `structuredContent`;
- полный вывод команды может сохраняться в серверном storage независимо от размера response window.

## Compact agent awareness

`work_scope` является optional metadata. Основная координация хранится в `details`, `current_step`, `intent` и message mailbox.

`RunResponse.active_agents` и `ReadResponse.active_agents` — `string[]`. Каждая строка:

```text
HH:MM:SS <public-name> <cmd_hash|started|finished> — <intent>
```

Active-awareness использует Session TTL 300 секунд; любой вызов с живым `agent_id` продлевает этот TTL. До первой команды активный агент отображается как `started`. После завершения сессии `finished` остаётся видимым 180 секунд. Внутренний suffix agent ID в этих строках не публикуется.

Scoped terminal line:

```text
HH:MM:SS <output>
```

Global terminal line:

```text
HH:MM:SS <public-name|anonymous> <cmd_hash> <output>
```
