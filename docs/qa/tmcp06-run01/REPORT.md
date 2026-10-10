# TMCP-06 / RUN-01 — независимая приёмка трёх live-исправлений

**UTC:** 2026-10-10, 23:00–23:10 (промежуточная приёмка до дальнейшей интеграции). **QA:** Secondary, legacy `Foxtrot-3`, собственная сессия; участок подтверждён `Zulu-2`, передан интегратору `TMCP-07 / Bravo-3`. **Scope:** опубликованные точные SHA, изолированные checkout, никаких правок реализации, canonical/production/runtime не изменены, full suite не запускался. Основной base: `bde0dec8bd0679c3cbda768104672de57956b6cf`.

## Remote и индивидуальные вердикты

GitHub `refs/heads` каждого кандидата сверены непосредственно с remote; `git fetch` в собственном checkout подтвердил точный FETCH_HEAD, HEAD отсоединён на SHA; base — предок, все рабочие деревья были чистыми. Подробности `logs/remote-attestation.log`.

| Кандидат | Точный опубликованный SHA | Приёмка | Логи и коды |
|---|---|---|---|
| LIVE021 Alpha-3 | `9ed2e8dd7e5d7fa7ce1b0113931c0014ebe6f59e` | **PASS** | 116 passed, pytest exit **0**, Ruff **0**, `git diff --check` **0**; `logs/live021-pytest.log`; MCP command `d8bf23a6` |
| Message Status Bravo-3 | `409040524bc444894a4f092035cd5e2bc906b5c2` | **PASS** | 160 passed, pytest exit **0**, Ruff **0**, diff **0**; `logs/message-pytest.log`; command `183a7ae3` |
| Tasklist History Charlie-3 | `8df8ef8623a00e4050258b78863142b206ead1d9` | **PASS** изменённых контрактов, с оговоркой baseline ниже | 134+124 passed в двух процессах, оба pytest exit **0**, Ruff **0**, diff **0**; `logs/task-history-pytest.log`, `logs/task-role-pytest.log`; command `a4832df8` |

Повторяющееся предупреждение Starlette/httpx о deprecated TestClient — единственное предупреждение в LIVE021, MESSAGE и cross-targeted. Независимое контрольное выполнение, а не только авторские отчёты.

## Отдельно: воспроизводимый baseline discovery hash-order FAIL

Контроль A/B в отдельных checkout, один и тот же пакет тестов (база без нового тестового файла):

| Проверка | Результат | Pytest exit | Лог |
|---|---|---|---|
| Кандидат Tasklist 8df8ef8, расширенный 14-файловый набор | **266 passed, 1 failed** | **1** | `logs/hash-order-candidate.log` |
| Чистая база bde0dec8, аналогичный набор без нового 12-case файла | **254 passed, 1 failed** | **1** | `logs/hash-order-base.log` |
| Только `test_all_seven_legacy_discovery_contracts_preserve_baseline` на базе | **1 passed** | **0** | `logs/hash-order-base-alone.log` |
| Только тот же тест на 8df8ef8 | **1 passed** | **0** | `logs/hash-order-task-alone.log` |

На обоих SHA отличается **только** legacy tool `session`, с **идентичным** фактическим SHA `022f41fa92151f1d0a717515e0418a709592789c15b66834218deaad9ee4a15b` против fixture `a976ffe5a7f5b2ef9868c2bf146b0cccb8dd5cf7f11e3cc13d8d194b8d3d4801`. Это подтверждённая существующая зависимость теста от порядка/коллекции, а не новая регрессия Tasklist. **Не считать широкий пакет green и не скрывать exit 1.** MCP command `7d162f6d`.

## Диагностическое объединение и перекрёстные регрессии

Три SHA объединены строго в QA-ветке `qa/TMCP-06-RUN01-foxtrot3-20261010`, начиная с base. SHA трёх-merge сборки до добавления QA-only тестов/документов: `bdafd09ef96187b4708ecf6cc1ae56ca1f14deb5`. Все три merges без конфликтов, diff-check 0, clean. Единственное совпадение по имени implementation-файла между ветками — `src/terminal_mcp/mcp/roles.py`: hunk установки LIVE021 diagnostic wrapper и hunk Tasklist `_task_get` находятся отдельно, auto-merged без ручного вмешательства. Message Status не перекрывает эти файлы. Конфликтов в тройке: **0**. Лог: `/workspace/tmcp06-qa-foxtrot3-20261010/evidence/qa-merge.log`.

- Перекрёстный **targeted** (15 конкретных тестовых модулей, без всей репозитории): **216 passed**, pytest exit **0**, 1 warning, Ruff exit **0**, diff-check exit **0**. `logs/cross-targeted.log`, `logs/cross-targeted.xml`, MCP command `e9aa1261`.
- Собственные `tests/test_tmcp06_foxtrot3_independent_qa.py`: **5 passed**, exit **0**, `logs/own-qa.log`, command `6ce0fc32`; пять случаев входят в 216: безопасная деградация diagnostic sink / whitelist receipt, исключение внутренних recipient IDs из wire, смешанный ACK/reply в broadcast, state-аудит без повышения content revision инвалидирует history cursor, одновременное сохранение MessageRecord.deliveries и role descriptions в QA merge.
- Эти QA-only тесты не требуют production, сетевого обращения к FirstByte/BacLOUD или изменения какой-либо реализации.

## Матрица изменённых контрактов

| Область | Принятая семантика | Что НЕ следует утверждать |
|---|---|---|
| LIVE021: success/error/cancel | Необязательная telemetry не маскирует возвращённый receipt и оригинальную ошибку/отмену; диагностические `mcp_http_{started,finished}` и `mcp_tool_{started,finished}` сохраняют безопасные попытки и известные receipt IDs | ASGI send и HTTP 200 не означают получение результата клиентом; JSON-RPC ID **не** idempotency key; два `command_run` не равны одному наблюдаемому повтору |
| Message sender history | Статусы outgoing читаются пакетно **после LIMIT** для recipient receipts, без дублирования cursor. Full включает `MessageRecord.deliveries` по адресатам, summary консервативно агрегирует | `sent` / `seen` / ACK / `reply` не взаимозаменяемы; факты доставки локальные, retention **24 часа**, без общего межсерверного transport |
| Message first_seen/read_at | `first_seen_at` = первый показ во включённом сервером ответе, в пользовательской терминологии «показано/прочитано»; `read_at` сохраняет legacy ACK-clock для ACK/alert, notify отмечается автоматически; `replied_at` отдельно | Сам факт показанного сервером сообщения не удостоверяет достижение клиента; ACK-gate сохраняется, универсальный read_at только для явного ACK не вводится |
| Tasklist defaults | Нефильтрованный `task_list` исключает done/archived; явный `state='done'` включает только незархивированные done; namespace/lane/state/tags сочетаются как фильтры | Возвращать старое требование «done обязано быть видно по умолчанию» неправильно |
| Task history | Fingerprint выбранного потока связывает cursor с текущим содержимым; изменение даёт `invalid_cursor` и требует restart; >100 events доступны через batch-скан | Content revision не является ключом валидности cursor: любой допущенный агент может менять state без content revision; `task_list` остаётся offset-пагинацией |

## Интеграционные зависимости и остаток

1. **Генератор:** `src/terminal_mcp/mcp/output_contracts.py` теперь объявляет `MessageRecord.deliveries`. После объединения с другими ветками общий интегратор **обязан пересобрать свежие схемы с финального integration SHA**; не использовать старые generated snapshots. Также проверить обновлённые role descriptions `task_list` и `task_get`.
2. **Другие исполнения:** OAuth, Events и generator имеют отдельных исполнителей. Их изменения в общий QA merge этой приёмки не включены; их текстовые/семантические конфликты проверяются общим интегратором отдельно. Особое внимание `app.py`/route tracing, где может оказаться другой middleware hunk. Не заявлять «полная интеграция» на основании этих трёх SHA.
3. **LIVE021:** не найден инициатор клиентского retry и hop потери ответа. Отдельные команды могут иметь одинаковый текст/JSON-RPC ID, поэтому запрещена спекулятивная дедупликация. Точный 82.951s caller wait не объясняется завершёнными proxy duration; нет доказательства конкретного слоя задержки. Для неопределённого write сначала читать task/receipt/state, потом принимать решение о повторе.
4. **Task history:** отдельная гонка записи во время многозапросной projection остаётся без атомарного cross-stream snapshot, fingerprint требует O(N) полной истории/памяти; task_list offset курсор не защищён от перестановок между страницами.
5. **Message:** aggregate для broadcast требует фактов всех адресатов, per-recipient факты не схлопывать; 24h retention и текущие ACK/alert-блокировки неизменны. Перед production требуется отдельно разрешённый live-read / transport acceptance.
6. **Вне области:** full repository suite, fleet release, production deployment и live mutation не выполнялись. Baseline hash-order FAIL сохраняется отдельным открытым QA-долгом.

**Решение:** три опубликованных исправления — **PASS для интеграции** в рамках проверенных контрактов; тройной диагностический merge — **PASS** на целевых cross-regressions. Финальный release gate требует общего интегратора, regenerated schemas и собственного интеграционного QA без подмены baseline FAILURE успехом.
