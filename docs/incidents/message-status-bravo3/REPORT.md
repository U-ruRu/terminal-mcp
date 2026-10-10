# Достоверность статусов локальных сообщений — Bravo-3

Дата: 10 октября 2026 UTC. Исполнитель: Bravo-3, Secondary. Координатор: X-ray-2 → Foxtrot-3. Результат: подтверждённый серверный дефект исправлен в изолированном worktree; **160 проверок прошли**, деплой не выполнялся.

## Основание и владение

Пользователь исходно поручил LIVE-021 (повторы команд, потерянный ответ после записи, задержки, duplicate_comment) двум агентам. По переназначению координатора в 21:35 UTC Alpha-3 получил всю LIVE-021, Bravo-3 — достоверность статусов сообщений. Материалы первого участка независимо опубликованы: ветка `task/TMCP-LIVE021-INGRESS-bravo3-20261010`, SHA `e5f3ab951c9d26d5278a6095a1455ceac33dc985`; remote проверен. Исходная задача не объявлена полностью решённой этим отчётом.

База изменения: `bde0dec8bd0679c3cbda768104672de57956b6cf`, local/remote `integration/M3-functional-candidate` совпали при старте. Ветка исправления: `task/TMCP-MESSAGE-STATUS-bravo3-20261010`. Код и 10 новых тестов: `9957779725646efd80474e8c58426c000130c6bc`. Worktree: `/workspace/terminal-mcp-message-status-bravo3-20261010`.

Shared-файлы согласованы с Foxtrot-3; Zulu-2 согласовал отделение от Events, Echo-3 подтвердил отсутствие конфликта с генератором. После интеграции генератор должен пересобрать схемы от нового integration SHA.

## Доказанный дефект

`AccessMeshMessageStore.page()` связывал `coordination_message_recipients` только с идентификатором читающего. При чтении исходящей истории читающим является отправитель, а строки получателя принадлежат адресатам. LEFT JOIN возвращал NULL, и `_view()` показывал `delivered`, пустые `first_seen_at/read_at/replied_at` и нулевой `seen_count` даже после реального чтения, ACK и ответа.

FirstByte исследован только через уже настроенный SSH, SELECT SQLite URI `mode=ro`. Runtime-store на live не создавался, команды/задачи/сообщения для воспроизведения на live не запускались. В repo-копии evidence внутренние logical-agent ID заменены стабильными метками; содержимое сообщений и OAuth-данные не экспортировались.

| Local message hash | Первое включение в ответ | ACK-clock read_at | Reply | Что возвращал JOIN отправителя |
|---|---|---|---|---|
| `firstbyte:localmsg:4656834d7d571a49d8bd29666bad8342` | 20:56:49.013Z | 20:56:54.864Z | 20:56:56.166Z | все три поля NULL |
| `firstbyte:localmsg:a23fc8e57c30ee97fdb2a5d26dd2113b` | 20:57:02.432Z | 20:57:16.633Z | 20:57:18.054Z | все три поля NULL |

Полные безопасные строки находятся в `firstbyte-status-evidence.txt`; программа SELECT — `collect_message_status_readonly.py`. Оба направления подтверждены. Времена относятся к 10 октября 2026 UTC.

## Исправление

1. Сообщения и cursor выбираются прежним способом: одна строка на сообщение. После LIMIT отдельным пакетным SELECT читаются факты получателей исходящих сообщений. Оба SELECT находятся в одной read-транзакции. Чтение отправителем не создаёт и не обновляет recipient-строку.
2. Для единственного адресата scalar-поля точно повторяют его durable receipt. Для нескольких адресатов состояние `seen/read/replied` достигается только после соответствующего факта у всех адресатов; timestamp — момент последнего из них. `last_seen_at` — последний показ любому адресату; scalar `seen_count` — минимум по адресатам. Единственный `reply_message_hash` сообщается только для одного адресата. Эти агрегаты не заменяют индивидуальные факты.
3. Full-история содержит additive `MessageRecord.deliveries`: public_name, state, first_seen_at, last_seen_at, seen_count, read_at, replied_at, reply_message_hash. Внутренние logical/recipient agent ID в публичный wire не попадают. Summary остаётся компактным и показывает консервативный общий статус.
4. Входящие сообщения отмечаются как показанные при первом включении в успешно проверенный серверный ответ и inbox, и history. Учитываются только действительно выданные bounded-page строки; лишняя строка для cursor/lookahead не меняется. Outgoing-строки той же history остаются read-only. Отказ response preflight оставляет факты неизменными.
5. Повторный показ сохраняет первое время, увеличивает счётчик фактических включений и не понижает `replied` до `read` для notify-сообщений.

## Совместимый смысл полей

По согласованию с dot существующие gates и legacy-wire значения сохранены. Пользовательское «прочитано/показано» соответствует **first_seen_at** (`state=seen` для ACK/alert до подтверждения). **read_at** для ACK/alert сохраняет прежний смысл выполнения ACK-обязательства; ответ также может выполнить это обязательство. Для notify это время автоматической отметки при показе. **replied_at** и **reply_message_hash** отдельно фиксируют ответ. `read_at` не является новым универсальным журналом только явных ACK.

Новая таблица ACK, миграция старых данных или переименование полей не вводились. ACK/alert-блокировки команд сохранены. First_seen означает включение в подготовленный серверный ответ; это не доказательство, что транспорт доставил ответ клиенту. Потерянные ответы и повторные RPC исследует Alpha-3 в LIVE-021.

## Проверки и точные команды

Все проверки выполнялись на временных SQLite двух изолированных test-узлов и безопасных fixture-сообщениях. Использован `/workspace/terminal-mcp/.venv-test/bin/pytest`, `PYTHONPATH=src` из указанного worktree.

- До исправления на исходной базе: `pytest -q tests/test_local_message_sender_receipts.py` — **2 failed**, exit **1**; проявились NULL первого показа и `seen_count=0` вместо 2. Файл `baseline-tests.log`, код в `baseline-tests.exit`.
- Первый storage-only вариант: 41 passed, exit 0. Расширенный промежуточный вариант: 93 passed, exit 0. Они являются промежуточными результатами, не итоговым объёмом приёмки.
- Итоговый целевой прогон на том же коде, который опубликован в `9957779`:

```sh
PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/pytest -q \
 tests/test_local_message_sender_receipts.py \
 tests/test_access_mesh_messages_v2.py \
 tests/test_local_messages_24h_victor.py \
 tests/test_mcp_output_contracts.py \
 tests/test_access_message_cmd_gates.py \
 tests/test_persistent_standalone_messaging.py \
 tests/test_persistent_fleet_messaging.py \
 tests/test_access_mesh_alias_self_message.py
```

**120 passed**, exit **0**, 27.29 s. `targeted.log`, `targeted.xml`, `targeted.exit`.

```sh
PYTHONPATH=src /workspace/terminal-mcp/.venv-test/bin/pytest -q \
 tests/test_event_journal.py tests/test_mcp_events.py \
 tests/test_read_contract.py tests/test_role_output_planning.py
```

**40 passed**, exit **0**, 7.15 s, на code SHA `9957779`. `integration-contracts.log`, `integration-contracts.xml`, `integration-contracts.exit`. Это проверки Events из принятой базы, а не независимая приёмка нового незавершённого кандидата Zulu-2.

Ruff check четырёх изменённых Python-файлов — exit 0; `git diff --check` — exit 0. Первый прогон имеет предупреждение Starlette/httpx deprecation; ошибка выполнения отсутствует.

Десять новых регрессий покрывают оба направления send/read/ACK/reply; неизменность outgoing-history; repeat-read; частичный broadcast; отсутствие дублей/пропусков cursor из-за нескольких получателей; incoming-history и lookahead; preflight fail; summary/full strict wire; restart/24h boundary; сохранение replied; изоляцию фактов других адресатов.

Полный глобальный pytest-suite и интеграция с будущими OAuth/Events/compact-изменениями в этой сессии **не выполнялись**. Локальная приёмка исправления не заменяет релизную приёмку объединённого кандидата.

## Артефакты, команды и безопасный следующий шаг

Артефакты Secondary: `/workspace/terminal-mcp-artifacts/message-status-bravo3-20261010/`. Live readonly collection и red-тест: command `d6e979f2`; targeted run `b2175f31`; code commit + integration-contract run `ed275ec8`. Их оболочки завершились exit 0, а ожидаемые failing pytest-коды отдельно записаны в файлы `*.exit`.

Изменены только `storage/access_mesh_messages.py`, `application/access_mesh_messages.py`, `mcp/output_contracts.py`, отдельный тестовый файл и эти документы. База данных, retention=24h, межсерверный transport, runtime/config/services, OAuth, генератор, чужие worktrees и `storage/events.py` сохранены.

Следующий шаг: dot/интегратор проверяет ветку, объединяет с принятыми кандидатами, пересобирает компактные output-схемы и проводит общую приёмку. Проверить full sender-history на отдельном pre-release экземпляре в обоих направлениях. Деплой FirstByte/BacLOUD требует отдельного решения; в этой работе он не выполнялся.
