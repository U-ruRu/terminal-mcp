---
name: terminal-operations
description: Выполняет серверные задачи через Terminal MCP Executor и Coordinator: sessions, tasks, messaging, commands, health и recovery.
compatibility: Terminal MCP 0.13.1; Executor v1 and Coordinator v1 role endpoints.
metadata:
  author: U-ruRu
  version: "2.1.0"
  language: ru
---

# Terminal operations

## Выбор роли

Executor: `/terminal-mcp/executor/v1/mcp` — команды, чтение задач, claim/state/comment, сообщения.
Coordinator: `/terminal-mcp/coordinator/v1/mcp` — управление задачами, граф, наблюдение агента, сообщения и health.

Используй инструменты выбранной роли из текущего discovery. Сервер получает identity и роль из доверенного контекста вызова.

## Рабочий цикл

Открой WorkSession через `session(action="start")`. Прочитай `task_list`; Coordinator уточняет карточку через `task_get`, Executor берёт её через `task_claim` с `claim_intent`. Выполняй команды через `command_run`; полученный `cmd_hash` дочитывай через `command_read` с opaque cursor. Проверяй конечный status и exit code. Сохраняй результаты через `task_comment`, `task_state` или Coordinator `task_manage`. Заверши сессию после сохранения рабочего состояния.

Coordinator `health` доступен до начала сессии. Компактный результат подходит для обычной проверки; `extended=true` добавляет диагностику.

## Жизненный цикл

LogicalAgent сохраняет identity. Автоматически созданная managed WorkSession имеет ограниченный TTL; её claims освобождаются при end, interrupt и expiry. Явно созданные persistent slots используют отдельную политику владения. Checkpoint и история задачи остаются доступными следующей сессии.

При lifecycle-ошибке сохрани доступный результат, выполни указанное recovery-действие и начни новую сессию, когда Session Gate разрешает старт. Повторно прочитай задачу перед новым claim. Используй актуальные серверные ревизии и session epoch.

## Команды и задачи

`command_run` принимает команду, optional `queue_id` и `task_scope`. Scope выбирай из `task_scope_options`: `none`, `all` или конкретный `namespace/task_id`. Очереди выполняются FIFO; завершившаяся быстро команда может вернуть первую страницу вывода сразу.

`command_cancel` отменяет собственную команду. `command_recovery` выполняет разрешённый recovery-путь. Чтение известного `cmd_hash` доступно без Access Code. Ограничивай вывод и проверяй завершение команды.

Состояния задач: `ready`, `in_progress`, `blocked`, `deferred`, `done`. Claim готовой задачи атомарно переводит её в `in_progress`. Сохраняй checkpoint, result, blocker_reason и release_reason. Dependency override использует содержательные `force_reason` и audit evidence.

## Сообщения

Выбирай адресата из `message(action="recipients")`. Отправляй по `public_name`, читай inbox через `read`, подтверждай через `ack`, отвечай через `reply`. Используй возвращённый `message_hash`. `history` продолжает журнал через opaque cursor. Broadcast задаётся `target="broadcast"` или отсутствием target; task addressing использует `namespace` и `task_id`.

Обрабатывай требуемые ACK и ответы на alerts перед дальнейшими мутациями.

## Ошибки и повторные вызовы

Ожидаемая ошибка приходит как `isError:false` и `ok:false` с объектом `error`. Используй code, details, outcome и retry для следующего действия. При неизвестном результате сначала прочитай фактическое состояние.

Строгая проверка работает на сервере; planning-схемы компактны. Повторение JSON-RPC ID `0` не обеспечивает exactly-once исполнение команд. Проверяй сохранённый command receipt перед повторным запуском с побочными эффектами.

## Репозиторий и релиз

Проверь `git status`, worktrees и canonical branch в project context. Продолжай существующую canonical работу, согласовав владение файлами с другими агентами. Сохраняй чужие изменения. Выполни focused tests, lint и общий набор перед релизом; коммить только свою область. Изолированную ветку используй при реальной необходимости и интегрируй проверенный результат в canonical.

Деплой ограничивай серверами, указанными в текущей задаче. Используй штатный installer/activation, сохрани rollback и durable state, проверь health, реальные tool calls и версии после переключения.

## Безопасность

Сохраняй секреты вне вывода и отчётов. Ограничивай изменения текущей задачей. Сохраняй живые claims других агентов; восстановление осиротевшего владения сопровождай проверкой сессии и audit evidence.

Legacy `/mcp` остаётся compatibility surface. Его каталог и Access Code сценарии описаны в [references/tool-contract.md](references/tool-contract.md).
