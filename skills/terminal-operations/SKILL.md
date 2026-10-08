---
name: terminal-operations
description: Работает с Linux-серверами через Terminal MCP Access Mesh V2: выдача доступа на Access, однократный attach к Executor/Coordinator, задачи, команды, сообщения и проверка результата.
compatibility: Terminal MCP 0.14.0; Access v1, Executor v1, Coordinator v1; Python runtime server-side.
metadata:
  author: U-ruRu
  version: "3.0.0"
  language: ru
---

# Terminal operations

Используй для фактической работы на подключённом сервере: изучения репозитория, выполнения команд, координации задач и проверки живого сервиса. Сначала прочитай контекст проекта и состояние сервера. Сохраняй существующие ограничения на серверы, worktree, ветку и деплой.

## Коннекторы и доступ

Access: `/terminal-mcp/access/v1/mcp`, один инструмент `session`.
Executor: `/terminal-mcp/executor/v1/mcp`, десять инструментов выполнения/задач/сообщений.
Coordinator: `/terminal-mcp/coordinator/v1/mcp`, восемь инструментов задач/наблюдения/сообщений/health.
На FirstByte и BacLOUD установлены одинаковые три первичных контракта: всего шесть коннекторов. Точные каталоги и валидные JSON-примеры находятся в [tool-contract.md](references/tool-contract.md).

Получи слот на выбранном Access через `session(action="start", mode="legacy")`. Persistent-слот создаёт оператор; для его активации используй Access `session(action="start", mode="persistent", code=...)`. Сохрани полученные issuer_node_id и access_code только в рабочем контексте. Виды слотов — legacy и persistent; kind неизменен, Mobile/Console управляет теми же слотами.

На каждом нужном Executor/Coordinator выполни единственный первоначальный `session(action="attach", issuer_node_id=..., access_code=...)`. Квалифицированный `issuer:dddd` также подходит. Четырёхзначный код разрешается внутри issuer. Повторный attach того же слота идемпотентен; другой слот в существующей привязке вызывает binding conflict.

Role session = attach-only. Последующие команды, задачи и сообщения получают identity из доверенного контекста коннектора и вызываются без Access Code. Завершение issuer-сессии передаётся исходному Access. Состояние ролей наблюдай через доступные read-инструменты, а не выдуманные session status/start/end/detach действия роли.

## Задача и команда

Прочитай актуальную задачу и checkpoint, проверь владельца и revision. Coordinator создаёт и изменяет задачу; Executor делает claim/release. Claim требует краткого claim_intent. Перед записью кода выбери отдельный worktree/ветку и согласуй границы с уже работающими агентами.

Claim lifecycle сохраняет state, checkpoint и result. Явно установи in_progress через task_state либо Coordinator task_manage(action="state"). Обновление свойств action=update сохраняет state. Сохраняй промежуточный checkpoint через Executor task_comment(action="checkpoint") либо Coordinator task_manage(action="checkpoint"); action="comment" добавляет запись истории. Выполни команды с подходящим task_scope из ответа сервера, прочитай вывод до терминального статуса, проверь exit_code и фактический результат. Для завершения передай проверяемый result через явное state=done/action=done.

Читай известный cmd_hash через command_read без Access Code, в том числе для команды другого LogicalAgent. Вариант command_read без cmd_hash возвращает локальный журнал всех агентов с identity metadata. Предварительный attach для этих чтений не требуется; настроенная транспортная аутентификация сохраняется. Успешный запуск команды подтверждает только запуск: тест, Git diff, health или readback должен подтвердить нужный эффект.

Используй limit/cursor и компактный detail по умолчанию. Продолжай opaque next_cursor без преобразования. При усечении нужного результата прочитай следующую страницу. Не выводи токены, private keys, Access Codes, raw provider identifiers и содержимое секретных конфигураций.

## Локальные циклы и продолжение

WorkSession и write gate локальны на каждом execution-сервере; LogicalAgent/public_name общие между ролями и серверами. Реплицированные policy/deadline определяют duration/cooldown/rearm/warning/draining. Повторные вызовы сохраняют deadline. При rearm новый локальный цикл создаётся по политике без повторного attach и issuer RPC на каждый вызов.

В draining сохрани checkpoint и выполни разрешённую финализацию. cleanup_pending означает незавершённое fencing/освобождение старой работы; опирайся на runtime retry и сохранённый checkpoint. Legacy claims снимаются при end/expiry; persistent claims — при release_on_end=true. Suspend/delete запускают отзыв и cleanup. Явное состояние задачи остаётся прежним.

При исчерпании собственной сессии сохрани проверенный SHA, логи, оставшиеся действия и ограничения. Продолжай через предусмотренную сервером смену/перевооружение сессии. Общая задача завершается по критериям приёмки, а не по окончанию одной агентской сессии.

## Сообщения и ошибки

Найди адресата через message(action="recipients"); используй стабильный public_name. local ограничивает сервер, fleet является scope по умолчанию. Рассылка исключает отправителя, повторные LogicalAgent и истёкшие локальные сессии. Локальное принятие фиксируется до обращения к peer. Для queued/partial сохрани message_hash и pending_peers: durable outbox повторит доставку после восстановления.

Read/history/ACK используют локальное состояние. Notify считается прочитанным после успешной выдачи страницы. Mode ack требует ACK; alert требует reply. Ответ и освобождение локального обязательства атомарны. Проверь обязательства перед следующей командой; ACK не заменяет reply для alert.

Inputs use permissive planning schemas; типы, обязательность и условия проверяются runtime. Handled errors приходят с isError:false, ok:false и структурированным error. Используй фактические code/details/outcome/retry/reason/path. Различай committed и not_committed; при неизвестном исходе сначала сверяй persisted state. Повторный JSON-RPC ID не склеивает разные task payload. Для shell constant ID не гарантирует exactly-once: сначала сверяй cmd_hash/journal.

## Проверка и передача результата

Фиксируй коммит только после тестов и проверки diff. Перед деплоем закрепи точный SHA, конфигурацию, резервные копии и ограничения восстановления. Текущий проект Terminal MCP разворачивается и проходит живую приёмку только на FirstByte и BacLOUD; Secondary служит разработке. Проверяй все шесть первичных коннекторов, межсерверные сообщения, задачи, expiry/cleanup и read-функции. Legacy /mcp проверяется дополнительно.

Обозначай выполненные проверки и оставшиеся ограничения отдельно. Закрывай задачу только после требуемых деплоя и приёмки. Сохраняй проверенный результат в checkpoint/handoff и в task result; не подменяй отсутствие живого теста прохождением unit tests.
