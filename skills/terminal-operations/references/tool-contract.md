# Контракт терминального инструмента

## Интерфейс

Подключённый terminal MCP предоставляет методы `agent_start`, `agent_task`, `agents`, `agent_finish`, `health`, `run`, `read`, `cancel` и `recovery`. `agent_start` и `agent_task` создают 180-секундный task lease для `run`; Agent Session TTL составляет 300 секунд. Конкретное пространство имён и способ подключения определяются текущим контекстом сервера и описанием доступных инструментов.

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

## `health()`

Аргументов нет. Возвращает состояние приложения, storage, авторизации, terminal adapter и FIFO-планировщика:

- `ok`, `application`, `storage`, `auth_mode`;
- `terminal.ok`, `user`, `uid`, `gid`, `cwd`, `privilege`, `shell`, `terminal_user`;
- `terminal.scheduler`, `parallelism`, `queue_size`, `running_commands`;
- необязательный `custom_command` с `command`, `lines`, `status`, `exit_code`, `error`, `ok`, `duration_ms`.

Настроенная health-команда выполняется отдельно от пользовательской FIFO-очереди.

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

`agent_task` принимает только `agent_id` и `intent` (1–160 символов). Scope берётся из `agent_start`.

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
