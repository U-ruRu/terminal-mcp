# Независимая приёмка compact V3 — FAIL / HOLD

Дата: 11 октября 2026, UTC. Ревизор: Hotel-4.

Проверен опубликованный серверный SHA `d96b682045744a9127140bf8b494afecd2a33184`, ветка `task/TMCP-COMPACT-RESPONSES-yankee3-20261011`. Исполнитель — Echo-4, ранее Yankee-3. Ревизор не менял реализацию. Новые файлы этой QA-ветки содержат только тесты, матрицу, примеры и отчёт.

Изначальная база задания: `83677e52660d8c1cc21c10ac0ce59bf6b1fa1b82`. Remote canonical при проверках 15:46 и 15:56 UTC: `a1a2a45f0199a2e50fbc322f938068e00bf76823`. Приёмка standalone V3 не является приёмкой объединения с новым canonical. Рабочая копия `/opt/terminal-mcp-event-integration` не изменялась.

## Итог тестов

**112 случаев: 100 PASS, 10 FAIL, 2 SKIP; pytest exit 1.** Один итоговый целевой прогон, 32.85 секунды. Это не полный suite финального объединённого кандидата. Лог, код выхода и JUnit сохранены в `acceptance-d96b682.{log,exit,xml}`.

Ранние прогоны: 96 PASS / 2 SKIP на исходном независимом наборе; затем 98 PASS / 10 FAIL / 2 SKIP с новыми отрицательными случаями. Это повторные прогоны пересекающихся наборов; их числа не суммируются. Оба пропуска относятся к отдельному intent, отсутствующему в standalone V3.

Ruff production, Ruff новых тестов, проверка десяти генерируемых компактных артефактов, каталог Events, repository privacy и whitespace-check: PASS. Генератор подтверждает актуальность файлов, а не уменьшение суммарного размера discovery-схем.

## H4-B1 — дублирование сообщений в отрицательных ответах

Четыре воспроизводимых FAIL: `command_run` и `task_claim`, каждый с обязательством ACK и reply. Реальный `SessionGate.command_state` выдаёт корректный отказ. Публичный адаптер уже выводит правильный корневой `messages`, но сохраняет то же сообщение в `error.details.pending_messages` с прежними `message_hash` и `mode`. Там остаются `ack_required_pending` и `alert_pending`, включая неиспользуемое значение `false`. Первое сообщение дополнительно повторяется в корневых `message_hash`, `text`, `required_action`.

Источник: `src/terminal_mcp/mcp/role_outputs.py`, `role_error_result`, строки 378–405 в проверенном SHA. Ошибка воспроизводится через настоящий публичный Executor tool boundary. ID и действие при коротком тексте сохраняются; потери сообщений или неявного ACK здесь не установлено.

Исправление: оставить единственное корневое представление входящих сообщений. Удалить дублирующие служебные поля из публичного error repair и корня после построения `messages`. Сохранить `error.code`, `message`, `outcome`, `retry` и реально необходимые данные восстановления. Изменить публичную схему и генерируемые артефакты синхронно. Серверные правила подтверждения остаются прежними.

## H4-B2 — допустимое сообщение превращает отказ в ошибку запроса

Шесть воспроизводимых FAIL: ACK/alert с текстом длиной 501 символ, с 16 384 символами Unicode и с публичным именем `Alpha-7 ` плюс разрешённый 70-символьный суффикс слота. Положительные контроли с текстом 500 символов и именем 64 символа проходят.

Цепочка: настоящий `SessionGate.command_state` → `cmd_result` → `normalize_public_error` → `_repair` → `CoordinationMessage`. Модель repair ограничивает `text` 500 символами и `sender` 64 символами. Производитель передаёт полный сохранённый текст и штатное публичное имя. Внутренняя Pydantic-валидация срабатывает при сериализации уже сформированного отказа. Внешний ответ получает `error.code=validation_error` и предложение исправить `text`/`sender`, которых нет среди аргументов `command_run`. Исходные `coordination_ack_required`/`coordination_alert`, ID сообщения и требуемое действие исчезают.

Источник: `src/terminal_mcp/core/public_errors.py`, `CoordinationMessage` и `_repair` (строки 372–376, 539–573); `core/persistent_backend.py`, `_persistent_message_entry`; `application/session_gate.py`, `command_state`. Эти ограничения существовали до V3; задача приёмки нового контракта выявила незакрытый путь отрицательного ответа. Оснований считать дефект внесённым именно V3 нет.

Исправление: разнести полное хранимое сообщение и ограниченную публичную проекцию до валидации старого repair. Сохранить штатное имя целиком, корректный код отказа, ID и requirements. Ограниченный preview требует явного предупреждения и рабочего чтения полного текста по ID. Нельзя выдавать внутреннюю ошибку сериализации за ошибку аргументов вызывающего агента.

## Матрица требований

| Область | Статус | Доказательство и граница |
|---|---|---|
| Обычный command_run | PASS | Точные корневые поля; пять действующих статусов; remaining_seconds=0; без ожидания статуса/вывода |
| command_read | PASS | Тело команды, stdout/stderr, отмена и завершение, exit_code, обрезание/сохранность, курсор |
| Входящие в успешных ответах | PASS | id/sender/text, ack/reply, дедупликация aliases, нет неявного ACK/reply |
| Входящие в ошибках | FAIL | H4-B1 и H4-B2 |
| Минимальный task_claim | PASS | Обычный результат ровно {"ok":true}; непустые уведомления условны |
| Ошибки task_claim/command_run | FAIL | Дубли служебных полей; допустимые сообщения теряют код и ID |
| Конфликт ревизии | PASS | Правильная ревизия для восстановления, отсутствие изменения задачи/claim |
| task_get detail/snapshot | PASS | Удаление заданных полей; непустые служебные коллекции; исходный тип checkpoint |
| Пользовательские checkpoint/result/payload | PASS | Ключи, вложенные false/0/null и пустые коллекции сохранены; blind JSON parsing отсутствует |
| История событий и комментариев | PASS | Payload и persistent_mutation сохранены; comments не меняются; лишние оболочки удалены |
| Пагинация истории | PASS | task.next_cursor продолжает стабильный поток; изменение выбранного потока явно даёт invalid_cursor, требуется начать заново |
| Связи и review-требования | PASS | Дедупликация blocking/relations; существующие тесты актуального output-state/review gate проходят |
| Health | PASS | Короткая и расширенная проекции; hidden required components влияют на healthy |
| Полные имена на всех Mesh-путях | NOTTESTED | Проверен lookup существующих authority-имён; отдельный naming-кандидат есть в новом canonical, объединение не принято |
| Intent: warning и последующий write gate | NOTTESTED | Два skip на standalone; объединённый runtime/источник не запускался |
| Events | PASS / NOTTESTED | Генерируемый каталог PASS; совместимость финального объединения и подписок NOTTESTED |
| Контракты/генерируемые файлы standalone | PASS | Проверки синхронизации прошли; отрицательные случаи требуют изменения схем |
| Финальный полный прогон | NOTTESTED | Не назначен согласованный интегратор/база; полный suite не запускался |

## Фактический смысл сообщений и хранения

`PersistentBackend.message_state` формирует `messages` и `pending_messages` из одних входящих обязательств вызывающего агента. Legacy `AgentCoordinator.message_state` использует структурированные записи и строковое представление тех же записей. Статусов исходящей доставки в этих producer-массивах нет. Предварительная схема была размещена прежним независимым ревизором на доске до изменения реализации. Корневая дедупликация по ID сохраняет текущие обязательства; сериализация не подтверждает сообщения.

`TaskStore.latest_checkpoint_event` читает JSON-значение из сохранённого payload. Объект, список и строка имеют различимые исходные типы. Новая проекция должна переносить этот тип в `latest_checkpoint.checkpoint`. Строка, похожая на JSON, остаётся строкой. Старые записи и payload истории не переписываются.

V3 возвращает ограниченные preview входящих и предупреждение об ограничении. Хранимый полный текст сохраняется. Отдельный известный блокер чтения очень длинного Unicode-сообщения по специализированной операции не принят: в V3 отсутствует chunk-продолжение текста. Его исправление закреплено за отдельной задачей; чужая реализация не дублировалась.

## Примеры и несовместимые изменения

Файлы `fixtures/compact-response-before-hotel4.json` и `fixtures/compact-response-after-hotel4.json` содержат одинаковые фиксированные входные данные. Before взят из сохранённого независимого red-control старой базы; after заново получен через публичный adapter V3. Это сравнение JSON-представления, а не эксперимент времени исполнения.

`command_run`: 815 → 86 байт compact UTF-8 JSON.

```json
{"ok":true,"remaining_seconds":0,"cmd_hash":"aabbccdd","status":"queued","queue_id":3}
```

`task_claim`: 529 → 11 байт.

```json
{"ok":true}
```

Клиентам требуются изменения: корневые cmd_hash/status/queue_id/remaining_seconds/messages вместо прежних оболочек; второй вызов command_read для команды, вывода и exit_code; task_get для сведений после claim; latest_checkpoint.checkpoint вместо JSON-строки text; две коллекции связей вместо dependencies; extended=true для полной health-диагностики. Response detail/kind исчезают, request selectors остаются. После согласованного deploy необходимо обновить каталоги Executor/Coordination; Access обновляется в составе согласованного intent/имён/Events. Сейчас коннекторы и runtime не менялись.

## Интеграционная граница и координация

`git merge-tree` d96b682 + a1a2a45: шесть конфликтов только в генерируемых JSON (`before-coordinator`, `catalog-coordinator`, `catalog-executor`, `coverage`, `metrics`, `provenance`). `roles.py` и `role_contracts.py` текстово сливаются автоматически. Это не семантическая приёмка объединения. Итоговый интегратор должен пересоздать артефакты на согласованном SHA и выполнить один полный прогон с логом/exit/JUnit.

Собственный native claim проверен на задаче `terminal-mcp/TMCP-COMPACT-RESPONSES-QA-HOTEL4-20261011`. Старые claims и worktrees не захватывались и не редактировались. Во время новой ревизии исполнитель недоступен (`recipient_not_active`), dot не разрешается как адресат (`recipient_not_found`). Замечания помещены в checkpoint; подтверждённой доставки координатору нет. Вторая операция comment вернула idempotency_conflict; перечитывание задачи подтвердило отсутствие записи. Слепой повтор не выполнялся.

Итог: независимая приёмка V3 — FAIL / HOLD. Сначала исправить H4-B1/H4-B2 и передать новый опубликованный SHA ревизору. Затем согласовать с dot одного интегратора и актуальную базу с naming, intent, Events и чтением полного текста. Canonical не менялся, deploy отсутствует, runtime Secondary и чужие задачи/сообщения не использовались для тестов.

## Артефакты и воспроизведение

Secondary: `/workspace/terminal-mcp-artifacts/compact-qa-hotel4-20261011/`. Основные файлы: `acceptance-d96b682.log`, `.xml`, `.exit`, `provenance.txt`, `static-status.tsv`, `merge-tree-d96-a1a2.txt`, before/after JSON. Команда приёмки Terminal MCP: `a7d49244`.

Из этой QA-ветки, production source которой равен d96b682:

```sh
PYTHONPATH=src python -m pytest \
  tests/test_compact_response_independent.py \
  tests/test_compact_large_messages_independent.py \
  tests/test_compact_error_notices_independent.py \
  tests/test_task_history_cursor_stability.py \
  tests/test_task_checkpoint_append_boundary.py \
  tests/test_task_revision_side_effects.py \
  tests/test_compact_incoming_budget.py tests/test_task_refs.py \
  -q --tb=short --junitxml=acceptance-d96b682.xml
```

Текущие десять FAIL намеренно сохранены как независимые требования. Не ослаблять ожидания для получения зелёного прогона.
